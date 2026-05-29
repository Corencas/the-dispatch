"""
assistant.py — AI voice dispatch co-pilot for The Dispatch.

Runs as daemon threads alongside the existing save watcher and telemetry reader.
Listens for voice input (push-to-talk or wake word), transcribes via OpenAI Whisper,
reasons via Claude claude-sonnet-4-5, and responds via ElevenLabs TTS.

Thread model:
  - main thread: pystray (untouched)
  - PTT/wake-word thread: listens for voice, calls _process_voice_input()
  - _process_voice_input: Whisper → Claude → TTS (all in their own threads)
  - AssistantState: shared state, protected by threading.Lock
"""

import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import anthropic
import numpy as np
import openai
import sounddevice as sd
import soundfile as sf
from dotenv import load_dotenv
from tts import speak_elevenlabs

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

OPENAI_API_KEY    = os.getenv('OPENAI_API_KEY', '')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
PICOVOICE_KEY     = os.getenv('PICOVOICE_ACCESS_KEY', '')
PTT_KEY_ENV       = os.getenv('PTT_KEY', 'delete')
VOICE_MODE        = os.getenv('VOICE_MODE', 'PUSH_TO_TALK')
SERVER_URL        = os.getenv('SERVER_URL', 'http://127.0.0.1:5001')
DISCORD_TOKEN     = os.getenv('DISCORD_TOKEN', '')
DISCORD_ID        = os.getenv('DISCORD_ID', '')

CLIENT_DIR   = Path(__file__).parent
LOG_PATH     = CLIENT_DIR / 'dispatch.log'
PREFS_PATH   = CLIENT_DIR / 'preferences.json'
HISTORY_PATH = CLIENT_DIR / 'voice_history.json'

SAMPLE_RATE     = 16000
MAX_RECORD_SECS = 30
MIN_RECORD_SECS = 0.5
BRIEFING_COOLDOWN = 180  # seconds between proactive messages

PERSONALITIES = {
    'professional':      {'voice': 'en-US-GuyNeural'},
    'friendly':          {'voice': 'en-US-DavisNeural'},
    'tactical':          {'voice': 'en-US-TonyNeural'},
    'female_professional': {'voice': 'en-US-JennyNeural'},
}

SYSTEM_PROMPT = (
    "You are a gruff CB radio dispatcher for ATS/ETS2. Use the available tools to answer "
    "questions accurately. Never guess or make up job details — always call the appropriate "
    "tool. Speak naturally, no symbols, 1-3 sentences for simple answers, fluid speech for "
    "job lists. Use trucker lingo naturally. "
    "IMPORTANT: 'garage_city' in context is the driver's home base, NOT current position. "
    "When active_job=True use nav_dst as destination and treat the driver as en route. "
    "Use nav_src/nav_dst cities for actual location context, not garage_city."
)

TOOLS = [
    {
        "name": "get_job_recommendations",
        "description": "Get the best available freight jobs for the driver's current trailer and location",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_region": {"type": "string", "description": "Optional region/state to filter jobs by source city. Empty for all."},
                "sort_by": {"type": "string", "enum": ["income", "efficiency", "distance"], "description": "How to rank jobs"},
                "limit": {"type": "integer", "description": "Max jobs to return, default 3"},
            },
            "required": [],
        },
    },
    {
        "name": "get_truck_status",
        "description": "Get current truck telemetry: fuel level, speed, engine status, odometer",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_current_job",
        "description": "Get details about the driver's active delivery job",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_fleet_status",
        "description": "Get status of all hired drivers in the fleet",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_finances",
        "description": "Get current company finances: cash, loans, revenue",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_nearby_stops",
        "description": "Find nearby fuel stations, rest stops, or dealerships based on current location",
        "input_schema": {
            "type": "object",
            "properties": {
                "stop_type": {"type": "string", "enum": ["fuel", "rest", "dealership", "service"], "description": "Type of stop to find"},
            },
            "required": ["stop_type"],
        },
    },
]

# ── Logging ───────────────────────────────────────────────────────────────────

_log_handlers = [logging.StreamHandler()]
try:
    _log_handlers.append(logging.FileHandler(LOG_PATH, encoding='utf-8'))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ASSISTANT] %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=_log_handlers,
)
log = logging.getLogger('assistant')

# ── Preferences ───────────────────────────────────────────────────────────────

DEFAULT_PREFS = {
    'personality': 'professional',
    'wake_word_enabled': False,
    'wake_word_keyword': 'porcupine',
    'push_to_talk_key': 'delete',
    'preferred_cargo': [],
    'avoided_cargo': [],
    'preferred_min_distance': 0,
    'preferred_max_distance': 9999,
    'preferred_min_revenue': 0,
    'target_weekly_earnings': 0,
    'preferred_cities': [],
    'avoided_cities': [],
    'notes': '',
}

_prefs_lock = threading.Lock()


def load_prefs() -> dict:
    with _prefs_lock:
        if PREFS_PATH.exists():
            try:
                with open(PREFS_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                prefs = {**DEFAULT_PREFS, **data}
            except Exception as e:
                log.warning(f"Failed to load preferences: {e}")
                prefs = dict(DEFAULT_PREFS)
        else:
            prefs = dict(DEFAULT_PREFS)
        # PTT_KEY in .env always overrides whatever is stored in preferences.json
        if PTT_KEY_ENV:
            prefs['push_to_talk_key'] = PTT_KEY_ENV
        return prefs


def save_prefs(prefs: dict):
    with _prefs_lock:
        try:
            with open(PREFS_PATH, 'w', encoding='utf-8') as f:
                json.dump(prefs, f, indent=2)
        except Exception as e:
            log.error(f"Failed to save preferences: {e}")


# ── Voice history ─────────────────────────────────────────────────────────────

_history_lock = threading.Lock()


def _append_history(user_text: str, assistant_text: str):
    entry = {
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'user': user_text,
        'assistant': assistant_text,
    }
    with _history_lock:
        try:
            history = []
            if HISTORY_PATH.exists():
                with open(HISTORY_PATH, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            history.append(entry)
            history = history[-100:]
            with open(HISTORY_PATH, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to write history: {e}")

    # Best-effort push to server (non-blocking)
    threading.Thread(target=_push_interaction_to_server, args=(entry,), daemon=True).start()


def _push_interaction_to_server(entry: dict):
    if not DISCORD_TOKEN or not DISCORD_ID:
        return
    try:
        import requests
        requests.post(
            f'{SERVER_URL}/api/interactions',
            json=entry,
            headers={
                'Authorization': f'Bearer {DISCORD_TOKEN}',
                'X-Discord-ID': DISCORD_ID,
            },
            timeout=5,
        )
    except Exception:
        pass  # Non-critical — history lives locally too


# ── Shared state ──────────────────────────────────────────────────────────────

class AssistantState:
    """
    Thread-safe shared state. The client loop writes; the assistant reads.
    Never holds the lock during API calls.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.telemetry: dict       = {}
        self.snapshot: dict        = {}
        self.freight_market: list  = []
        self.last_briefing: float  = 0.0    # monotonic clock
        self.session_start_snap: dict = {}

        # Proactive trigger tracking
        self._prev_job_count: int  = -1
        self._fuel_warned: bool    = False
        self._session_briefed: bool = False

    def update_telemetry(self, t: dict):
        with self._lock:
            self.telemetry = dict(t) if t else {}

    def update_snapshot(self, snap: dict, freight_market: list | None = None):
        with self._lock:
            if not self.session_start_snap and snap:
                self.session_start_snap = dict(snap)
            self.snapshot = dict(snap) if snap else {}
            if freight_market is not None:
                self.freight_market = list(freight_market)

    def read(self) -> tuple[dict, dict, list, float, dict]:
        """Return (telemetry, snapshot, freight_market, last_briefing, session_start_snap)."""
        with self._lock:
            return (
                dict(self.telemetry),
                dict(self.snapshot),
                list(self.freight_market),
                self.last_briefing,
                dict(self.session_start_snap),
            )

    def set_briefing_now(self):
        with self._lock:
            self.last_briefing = time.monotonic()


# Module-level instance — imported and written to by client.py and telemetry.py
state = AssistantState()

# City coordinate database — populated by client.py on startup via build_city_db()
city_db: dict = {}


# ── Context builder ───────────────────────────────────────────────────────────

def _fmt_money(n: int | float) -> str:
    return f'${int(n):,}'


def _filter_market(market: list, prefs: dict) -> list:
    """Filter and score the freight market by user preferences."""
    preferred = [p.lower() for p in prefs.get('preferred_cargo', [])]
    avoided   = [a.lower() for a in prefs.get('avoided_cargo', [])]
    min_rev   = prefs.get('preferred_min_revenue', 0)
    min_dist  = prefs.get('preferred_min_distance', 0)
    max_dist  = prefs.get('preferred_max_distance', 9999)
    pref_cities = [c.lower() for c in prefs.get('preferred_cities', [])]
    avoid_cities = [c.lower() for c in prefs.get('avoided_cities', [])]

    results = []
    for j in market:
        cargo = j.get('cargo', '').lower()
        dist  = j.get('distance_km', 0)
        rev   = j.get('revenue', 0)
        src   = j.get('source_city', '').lower()
        dst   = j.get('destination_city', '').lower()

        if any(av in cargo for av in avoided):
            continue
        if any(ac in src or ac in dst for ac in avoid_cities):
            continue
        if rev < min_rev or dist < min_dist or dist > max_dist:
            continue

        score = rev
        if preferred and any(p in cargo for p in preferred):
            score += 50_000
        if pref_cities and any(pc in dst for pc in pref_cities):
            score += 20_000

        results.append((score, j))

    results.sort(key=lambda x: x[0], reverse=True)
    return [j for _, j in results]


# ── Tool handlers ─────────────────────────────────────────────────────────────

def handle_tool_call(tool_name: str, tool_input: dict, snapshot: dict, telemetry: dict):
    if tool_name == "get_job_recommendations":
        return _tool_job_recommendations(tool_input, snapshot)
    elif tool_name == "get_truck_status":
        return _tool_truck_status(telemetry)
    elif tool_name == "get_current_job":
        return _tool_current_job(snapshot)
    elif tool_name == "get_fleet_status":
        return _tool_fleet_status(snapshot)
    elif tool_name == "get_finances":
        return _tool_finances(snapshot)
    elif tool_name == "find_nearby_stops":
        return _tool_nearby_stops(tool_input, telemetry)
    return {"error": f"Unknown tool: {tool_name}"}


def _tool_job_recommendations(tool_input: dict, snapshot: dict) -> dict:
    prefs = load_prefs()

    freight_market = snapshot.get("freight_market", [])
    if isinstance(freight_market, dict):
        freight_market = freight_market.get("offers", [])
    if not isinstance(freight_market, list):
        freight_market = []

    trailer = snapshot.get("trailer", {}) or {}
    trailer_body_type = trailer.get("body_type")
    try:
        from parser import job_matches_body_type as _jmbt
    except ImportError:
        _jmbt = None

    total = len(freight_market)
    fallback_note = None

    if trailer_body_type:
        matched = [
            j for j in freight_market
            if j.get("body_type") == trailer_body_type
            or trailer_body_type in (j.get("trailer_def") or "")
            or (bool(_jmbt) and _jmbt(j.get("trailer_def", ""), trailer_body_type))
        ]
        log.info(f"Tool job_recommendations: trailer={trailer_body_type!r}, matched={len(matched)}/{total}")
        if matched:
            candidates = matched
        else:
            log.info("Tool job_recommendations: zero trailer matches — falling back to full market")
            candidates = freight_market
            fallback_note = f"No trailer-specific jobs found for {trailer_body_type}, showing general market."
    else:
        log.info(f"Tool job_recommendations: no trailer type — using full market ({total} jobs)")
        candidates = freight_market

    filtered = _filter_market(candidates, prefs)

    # Optional region filter
    region = (tool_input.get("filter_region") or "").lower().strip()
    if region:
        filtered = [j for j in filtered if region in j.get("source_city", "").lower()]

    sort_by = tool_input.get("sort_by", "income")
    if sort_by == "distance":
        filtered.sort(key=lambda j: j.get("distance_km", 0))
    elif sort_by == "efficiency":
        filtered.sort(
            key=lambda j: j.get("revenue", 0) / max(j.get("distance_km", 1), 1),
            reverse=True,
        )
    # "income" is already sorted descending by _filter_market

    limit = tool_input.get("limit", 3)
    top = filtered[:limit]

    if not top:
        return {"jobs": [], "message": "No matching jobs in freight market."}

    result: dict = {
        "jobs": [
            {
                "cargo":          j.get("cargo", "unknown"),
                "source":         j.get("source_city", "?"),
                "destination":    j.get("destination_city", "?"),
                "income":         j.get("revenue", 0),
                "distance_miles": round(int(j.get("distance_km", 0)) * 0.621371),
                "market":         j.get("market", "unknown"),
            }
            for j in top
        ]
    }
    if fallback_note:
        result["note"] = fallback_note
    return result


def _tool_truck_status(telemetry: dict) -> dict:
    speed_kmh = telemetry.get("speed_kmh", 0)
    fuel_pct  = telemetry.get("fuel_pct", 0)
    truck = f"{telemetry.get('truck_make', '')} {telemetry.get('truck_model', '')}".strip()
    return {
        "speed_mph":      round(speed_kmh * 0.621371),
        "fuel_pct":       round(float(fuel_pct)),
        "gear":           telemetry.get("gear", 0),
        "engine_on":      telemetry.get("engine_on", False),
        "odometer_miles": round(telemetry.get("odometer_km", 0) * 0.621371),
        "cargo":          telemetry.get("cargo", ""),
        "truck":          truck or "unknown",
    }


def _tool_current_job(snapshot: dict) -> dict:
    player = snapshot.get("player", {}) or {}
    trailer = snapshot.get("trailer", {}) or {}

    job_cargo = (player.get("job_info_cargo") or "").strip()
    try:
        job_dist_km = int(player.get("job_info_planned_distance_km") or 0)
    except (TypeError, ValueError):
        job_dist_km = 0

    active = bool(job_cargo and job_cargo not in ("null", "nil") and job_dist_km > 0)
    log.info(f"Tool current_job: job_info_cargo={job_cargo!r} dist_km={job_dist_km} -> active={active}")

    if not active:
        return {"active": False, "message": "No active job."}

    job_target  = player.get("job_info_target") or trailer.get("active_destination") or ""
    job_cargo   = player.get("job_info_cargo") or "unknown"
    job_urgency = player.get("job_info_urgency", "0") or "0"

    dest_parts = job_target.split(".") if job_target else []
    dest_city  = dest_parts[-1].replace("_", " ").title() if dest_parts else "unknown"
    company    = dest_parts[1].replace("_", " ").title() if len(dest_parts) > 1 else ""

    return {
        "active":         True,
        "cargo":          job_cargo,
        "destination":    dest_city,
        "company":        company,
        "distance_miles": round(job_dist_km * 0.621371),
        "urgency":        job_urgency,
    }


def _tool_fleet_status(snapshot: dict) -> dict:
    drivers = snapshot.get("drivers", [])
    return {
        "total": len(drivers),
        "drivers": [
            {
                "name":   d.get("name", "unknown"),
                "city":   (d.get("city") or d.get("current_city", "unknown")).replace("_", " ").title(),
                "status": "on_job" if d.get("state") == 2 else "idle",
            }
            for d in drivers[:10]
        ],
    }


def _tool_finances(snapshot: dict) -> dict:
    fin  = snapshot.get("finances", {}) or {}
    jobs = snapshot.get("jobs", [])
    money = fin.get("money", 0)
    debt  = fin.get("total_debt", 0)
    log.info(f"Tool finances: raw money={money!r}, raw debt={debt!r}")
    return {
        "cash":                   money,
        "debt":                   debt,
        "net_worth":              money - debt,
        "recent_5_jobs_revenue":  sum(j.get("revenue", 0) for j in jobs[:5]),
    }


def _tool_nearby_stops(tool_input: dict, telemetry: dict) -> dict:
    stop_type = tool_input.get("stop_type", "fuel")
    pos_x = telemetry.get("pos_x", 0)
    pos_z = telemetry.get("pos_z", 0)

    if not city_db or (pos_x == 0 and pos_z == 0):
        return {"message": f"No GPS position available — check in-game map (F8) for {stop_type} stops."}

    ranked = []
    for c in city_db.values():
        cx = c.get('x')
        cz = c.get('z')
        if cx is None or cz is None:
            continue
        d2 = (pos_x - cx) ** 2 + (pos_z - cz) ** 2
        name  = c.get('name', '?')
        state = c.get('state', '')
        ranked.append((d2, f"{name}, {state}" if state else name))
    ranked.sort(key=lambda x: x[0])

    nearby = [label for _, label in ranked[:4]]
    return {
        "stop_type":      stop_type,
        "nearest_cities": nearby,
        "note":           "Exact stop locations shown on in-game map (F8). These are the closest cities by GPS.",
    }


# ── Claude API ────────────────────────────────────────────────────────────────

_anthropic_client: anthropic.Anthropic | None = None
_anthropic_lock = threading.Lock()

_conv_history: list[dict] = []   # in-memory multi-turn history, capped at 6 messages
_conv_lock = threading.Lock()


def _get_anthropic() -> anthropic.Anthropic | None:
    global _anthropic_client
    with _anthropic_lock:
        if _anthropic_client is None and ANTHROPIC_API_KEY:
            _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        return _anthropic_client


_FALLBACK = {
    'api_error': "Copy that. Brief comms interruption — standing by.",
    'no_key':    "AI dispatch is offline. Add ANTHROPIC_API_KEY to client/.env to enable it.",
}


def query_claude(user_message: str, prefs: dict,
                 telemetry: dict, snapshot: dict,
                 freight_market: list, session_start_snap: dict) -> str:
    global _conv_history

    client = _get_anthropic()
    if not client:
        log.warning("ANTHROPIC_API_KEY not set")
        return _FALLBACK['no_key']

    # ── Diagnostic logging — runs on every query ─────────────────────────────
    player = snapshot.get("player", {}) or {}
    log.info(f"DIAG player (full): {player!r}")
    log.info(f"DIAG telemetry pos=({telemetry.get('pos_x')},{telemetry.get('pos_z')}) "
             f"nav_src={telemetry.get('nav_src_city')!r} nav_dst={telemetry.get('nav_dst_city')!r} "
             f"on_job={telemetry.get('on_job')}")

    # ── Minimal system context (no big JSON dump) ─────────────────────────────
    # garage_city = home base from save file (NOT current truck position)
    garage_city = (
        player.get("current_city")
        or snapshot.get("current_city")
        or snapshot.get("city")
        or "unknown"
    )

    trailer = snapshot.get("trailer", {}) or {}
    trailer_type = trailer.get("body_type") or "unknown"

    # Live nav cities from telemetry SDK (most accurate location data)
    nav_dst_city = telemetry.get("nav_dst_city") or ""
    nav_src_city = telemetry.get("nav_src_city") or ""
    pos_x        = telemetry.get("pos_x", 0)
    pos_z        = telemetry.get("pos_z", 0)

    # Active job detection — job_info_cargo + job_info_planned_distance_km are
    # more reliable than in_job which can lag or be absent in save files.
    live_cargo      = (telemetry.get("cargo") or "").strip()
    live_cargo_mass = float(telemetry.get("cargo_mass") or 0)
    job_info_cargo = (player.get("job_info_cargo") or "").strip()
    try:
        job_info_dist = int(player.get("job_info_planned_distance_km") or 0)
    except (TypeError, ValueError):
        job_info_dist = 0
    has_active_job = bool(
        live_cargo_mass > 0
        or live_cargo
        or (job_info_cargo and job_info_cargo not in ("null", "nil") and job_info_dist > 0)
    )
    log.info(
        f"DIAG active_job: live_cargo={live_cargo!r} mass={live_cargo_mass} "
        f"job_info_cargo={job_info_cargo!r} job_info_dist={job_info_dist} "
        f"-> has_active_job={has_active_job}"
    )

    # Build driver context string for Claude's system note
    if has_active_job:
        job_target = player.get("job_info_target") or ""
        dest_parts = job_target.split(".") if job_target else []
        dest_city  = dest_parts[-1].replace("_", " ").title() if dest_parts else nav_dst_city or "unknown"
        location_ctx = f"en_route_to={dest_city}"
    else:
        if (pos_x != 0 or pos_z != 0) and city_db:
            try:
                from city_db import get_nearest_city as _gnc
                nearest = _gnc(pos_x, pos_z, city_db)
            except Exception:
                nearest = None
            if nearest:
                location_ctx = f"nearest_city={nearest}, garage_city={garage_city}"
            else:
                location_ctx = f"garage_city={garage_city}"
        else:
            location_ctx = f"garage_city={garage_city}"

    system = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Driver context: {location_ctx}, nav_src={nav_src_city or 'none'}, "
        f"nav_dst={nav_dst_city or 'none'}, pos=({pos_x:.0f},{pos_z:.0f}), "
        f"trailer={trailer_type}, active_job={has_active_job}"
    )

    log.info(f"Claude: user_message={user_message!r}")
    log.info(f"Claude: context {location_ctx} trailer={trailer_type!r} active_job={has_active_job}")

    with _conv_lock:
        recent = list(_conv_history[-6:])

    messages_payload = recent + [{'role': 'user', 'content': user_message}]

    try:
        log.info(f"Claude: sending request (model=claude-sonnet-4-6, history_msgs={len(recent)})")
        resp = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=400,
            system=system,
            tools=TOOLS,
            messages=messages_payload,
        )
        log.info(f"Claude: stop_reason={resp.stop_reason}")

        # ── Agentic tool-use loop ─────────────────────────────────────────────
        while resp.stop_reason == 'tool_use':
            tool_results = []
            for block in resp.content:
                if block.type == 'tool_use':
                    log.info(f"Claude: tool_call={block.name} input={block.input!r}")
                    result = handle_tool_call(block.name, block.input, snapshot, telemetry)
                    log.info(f"Claude: tool_result={result!r}")
                    tool_results.append({
                        'type':        'tool_result',
                        'tool_use_id': block.id,
                        'content':     json.dumps(result),
                    })

            messages_payload = messages_payload + [
                {'role': 'assistant', 'content': resp.content},
                {'role': 'user',      'content': tool_results},
            ]

            resp = client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=400,
                system=system,
                tools=TOOLS,
                messages=messages_payload,
            )
            log.info(f"Claude: follow-up stop_reason={resp.stop_reason}")

        text = " ".join(
            block.text for block in resp.content if hasattr(block, 'text')
        ).strip() or _FALLBACK['api_error']

        log.info(f"Claude: received {len(text)} chars")

        with _conv_lock:
            _conv_history.append({'role': 'user',      'content': user_message})
            _conv_history.append({'role': 'assistant', 'content': text})
            _conv_history = _conv_history[-6:]

        return text

    except anthropic.APIStatusError as e:
        log.error(f"Claude: APIStatusError status={e.status_code} message={e.message!r}",
                  exc_info=True)
        return _FALLBACK['api_error']
    except Exception as e:
        log.error(f"Claude: unexpected error type={type(e).__name__} repr={e!r}",
                  exc_info=True)
        return _FALLBACK['api_error']


# ── Preference voice editing ───────────────────────────────────────────────────

def _parse_pref_command(text: str, prefs: dict) -> tuple[dict, str | None]:
    """
    Detect preference-update intent in transcribed speech.
    Returns (updated_prefs, confirmation_msg | None).
    Only matches clear, unambiguous phrasing to avoid false positives.
    """
    low = text.lower().strip()

    if re.search(r'reset\s+(?:my\s+)?preferences?', low):
        new_prefs = dict(DEFAULT_PREFS)
        save_prefs(new_prefs)
        return new_prefs, "Preferences reset to defaults. Starting fresh."

    # "remember I don't like / avoid / never / hate hauling X"
    avoid_m = re.search(
        r"(?:don'?t|do not|avoid|never haul|hate hauling?|no more)\s+(?:hauling?\s+)?([a-z][a-z\s-]{2,30}?)(?:\s*[.,]|$)",
        low,
    )
    # "remember I prefer / like / want X"
    prefer_m = re.search(
        r"(?:prefer|i like hauling?|i want more|prioritize)\s+(?:hauling?\s+)?([a-z][a-z\s-]{2,30}?)(?:\s*[.,]|$)",
        low,
    )

    prefs = dict(prefs)

    if avoid_m:
        item = avoid_m.group(1).strip().rstrip('s')  # normalize plural
        if item and item not in prefs.get('avoided_cargo', []):
            prefs.setdefault('avoided_cargo', []).append(item)
            save_prefs(prefs)
            return prefs, f"Got it. I'll filter out {item} jobs for you."

    if prefer_m:
        item = prefer_m.group(1).strip().rstrip('s')
        if item and item not in prefs.get('preferred_cargo', []):
            prefs.setdefault('preferred_cargo', []).append(item)
            save_prefs(prefs)
            return prefs, f"Noted. I'll prioritize {item} loads going forward."

    return prefs, None


# ── TTS output ────────────────────────────────────────────────────────────────

tts_queue: queue.Queue = queue.Queue()

# Set to True once the session-start briefing has fully finished playing.
# PTT pipeline processing is blocked until then so the briefing never races
# with an early PTT press.
_startup_complete: bool = False

# Mute flag toggled by the overlay's MUTE button.
_tts_muted: bool = False


def _set_tts_muted(muted: bool):
    """Called by the overlay mute button to silence / restore TTS."""
    global _tts_muted
    _tts_muted = muted
    log.info(f"TTS: muted={muted}")


def speak(text: str, prefs: dict | None = None):
    """Enqueue text for TTS — never interrupts current speech."""
    tts_queue.put(text)


def _tts_worker():
    """Single dedicated daemon thread: speaks one queued item at a time, FIFO."""
    while True:
        text = tts_queue.get()
        if _tts_muted:
            log.info(f"TTS: muted — skipping ({len(text)} chars)")
            tts_queue.task_done()
            continue
        log.info(f"TTS: speaking ({len(text)} chars)")
        try:
            speak_elevenlabs(text)
        except Exception as e:
            log.error(f"TTS: speak_elevenlabs error: {e}", exc_info=True)
        finally:
            tts_queue.task_done()
        log.info("TTS: done, 2.5s gap before next item")
        time.sleep(2.5)


# ── Audio recording ───────────────────────────────────────────────────────────

def record_audio(stop_event: threading.Event, max_secs: int = MAX_RECORD_SECS) -> str | None:
    """
    Record from the default mic until stop_event fires or max_secs elapses.
    Returns path to a temp WAV file, or None if too short / on error.
    """
    chunks = []
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='int16') as stream:
            deadline = time.monotonic() + max_secs
            while not stop_event.is_set() and time.monotonic() < deadline:
                chunk, _ = stream.read(SAMPLE_RATE // 10)  # 100 ms
                chunks.append(chunk.copy())
    except Exception as e:
        log.error(f"Recording error: {e}")
        return None

    if not chunks:
        log.warning("record_audio: no audio chunks captured")
        return None

    audio = np.concatenate(chunks, axis=0)
    duration = len(audio) / SAMPLE_RATE
    log.info(f"record_audio: captured {duration:.2f}s of audio ({len(chunks)} chunks)")
    if duration < MIN_RECORD_SECS:
        log.warning(f"record_audio: too short ({duration:.2f}s < {MIN_RECORD_SECS}s minimum) — discarding")
        return None

    tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    tmp.close()
    try:
        sf.write(tmp.name, audio, SAMPLE_RATE)
        return tmp.name
    except Exception as e:
        log.error(f"WAV write error: {e}")
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return None


# ── Whisper transcription ─────────────────────────────────────────────────────

_openai_client: openai.OpenAI | None = None
_openai_lock = threading.Lock()


def _get_openai() -> openai.OpenAI | None:
    global _openai_client
    with _openai_lock:
        if _openai_client is None and OPENAI_API_KEY:
            _openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
        return _openai_client


def transcribe(wav_path: str) -> str | None:
    """Transcribe a WAV via OpenAI Whisper. Returns text or None."""
    client = _get_openai()
    if not client:
        log.warning("Whisper: OPENAI_API_KEY not set — transcription disabled")
        return None
    try:
        size = os.path.getsize(wav_path) if os.path.exists(wav_path) else -1
        log.info(f"Whisper: sending {wav_path} ({size} bytes) to whisper-1")
        with open(wav_path, 'rb') as f:
            result = client.audio.transcriptions.create(
                model='whisper-1',
                file=f,
                language='en',
                response_format='text',
            )
        text = (result.strip() if isinstance(result, str) else str(result).strip())
        log.info(f"Whisper: transcript={text!r}")
        return text or None
    except openai.APIError as e:
        log.error(f"Whisper: API error status={e.status_code}: {e}", exc_info=True)
        return None
    except Exception as e:
        log.error(f"Whisper: unexpected error: {e}", exc_info=True)
        return None


# ── Voice pipeline ────────────────────────────────────────────────────────────

def _process_voice_input(wav_path: str):
    """
    Full pipeline: WAV → Whisper → pref-update or Claude → TTS → log.
    Runs in its own thread so it never blocks the hotkey listener.
    """
    if not wav_path:
        log.warning("Pipeline: wav_path is None/empty — nothing to process")
        return

    if not _startup_complete:
        log.info("Pipeline: startup briefing not yet complete — dropping PTT input")
        try:
            os.unlink(wav_path)
        except OSError:
            pass
        return

    try:
        # ── Step 1: log audio file info ───────────────────────────────────────
        try:
            size = os.path.getsize(wav_path)
            duration = size / (SAMPLE_RATE * 2)  # 16-bit mono = 2 bytes/sample
            log.info(f"Pipeline: audio file {wav_path} — {size} bytes, ~{duration:.1f}s")
        except Exception as e:
            log.error(f"Pipeline: could not stat audio file: {e}", exc_info=True)

        # ── Step 2: Whisper transcription ─────────────────────────────────────
        log.info("Pipeline: calling transcribe()")
        try:
            text = transcribe(wav_path)
        except Exception as e:
            log.error(f"Pipeline: transcribe() raised: {e}", exc_info=True)
            return
        log.info(f"Pipeline: transcribe() returned {text!r}")

        if not text:
            log.info("Pipeline: empty transcript — aborting (no speech detected)")
            return

        # Push transcript to overlay
        try:
            from overlay import overlay_state
            overlay_state["last_transcript"] = text
            overlay_state["transcript_time"] = time.monotonic()
        except Exception:
            pass

        prefs = load_prefs()

        # ── Step 3: preference-update shortcut ────────────────────────────────
        try:
            prefs, pref_msg = _parse_pref_command(text, prefs)
        except Exception as e:
            log.error(f"Pipeline: _parse_pref_command raised: {e}", exc_info=True)
            pref_msg = None

        if pref_msg:
            log.info(f"Pipeline: pref update matched — {pref_msg!r}")
            speak(pref_msg, prefs)
            _append_history(text, pref_msg)
            return

        # ── Step 4: Claude ────────────────────────────────────────────────────
        log.info(f"Pipeline: calling Claude with user text={text!r}")
        t0 = time.monotonic()
        try:
            tel, snap, market, _, start_snap = state.read()
            log.info(f"Pipeline: snapshot has keys={list(snap.keys())}, "
                     f"jobs={len(snap.get('jobs', []))}, "
                     f"freight_market={len(snap.get('freight_market', []))}, "
                     f"snapshot_empty={not snap}")
            response = query_claude(text, prefs, tel, snap, market, start_snap)
        except Exception as e:
            log.error(f"Pipeline: query_claude() raised type={type(e).__name__} repr={e!r}",
                      exc_info=True)
            return
        elapsed = time.monotonic() - t0
        log.info(f"Pipeline: Claude responded in {elapsed:.1f}s — {response!r}")

        # Push AI response to overlay (subtitle + history)
        try:
            from overlay import overlay_state
            overlay_state["last_response"]  = response
            overlay_state["response_time"]  = time.monotonic()
            overlay_state["history"].append({"role": "user",      "text": text})
            overlay_state["history"].append({"role": "assistant", "text": response})
            overlay_state["history"] = overlay_state["history"][-20:]  # keep last 10 turns
        except Exception:
            pass

        # ── Step 5: TTS ───────────────────────────────────────────────────────
        log.info(f"Pipeline: launching TTS for {len(response)} chars")
        try:
            speak(response, prefs)
        except Exception as e:
            log.error(f"Pipeline: speak() raised: {e}", exc_info=True)
            return
        log.info("Pipeline: TTS thread launched")

        _append_history(text, response)
        log.info("Pipeline: complete")

    except Exception as e:
        log.error(f"Pipeline: unhandled exception: {e}", exc_info=True)
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


# ── Push-to-talk ─────────────────────────────────────────────────────────────

# pynput key name → pynput.keyboard.Key mapping.
# pynput's Listener uses WH_KEYBOARD_LL in its own dedicated Win32 message-loop
# thread, which receives events from every application including full-screen games.
_PYNPUT_KEY_MAP: dict[str, str] = {
    'delete': 'delete', 'del': 'delete',
    'insert': 'insert', 'ins': 'insert',
    'home': 'home', 'end': 'end',
    'page up': 'page_up', 'pageup': 'page_up',
    'page down': 'page_down', 'pagedown': 'page_down',
    'f1': 'f1', 'f2': 'f2', 'f3': 'f3', 'f4': 'f4',
    'f5': 'f5', 'f6': 'f6', 'f7': 'f7', 'f8': 'f8',
    'f9': 'f9', 'f10': 'f10', 'f11': 'f11', 'f12': 'f12',
    'caps lock': 'caps_lock', 'num lock': 'num_lock',
    'scroll lock': 'scroll_lock', 'pause': 'pause',
    'left ctrl': 'ctrl_l', 'right ctrl': 'ctrl_r', 'ctrl': 'ctrl_l',
    'left alt': 'alt_l', 'right alt': 'alt_r', 'alt': 'alt_l',
    'left shift': 'shift_l', 'right shift': 'shift_r', 'shift': 'shift_l',
}


def _resolve_pynput_key(key_name: str):
    """Return a pynput Key enum member or KeyCode for the given name string."""
    from pynput.keyboard import Key, KeyCode
    low = key_name.lower().strip()
    attr = _PYNPUT_KEY_MAP.get(low)
    if attr:
        return getattr(Key, attr, None)
    # Single printable character
    if len(low) == 1:
        return KeyCode.from_char(low)
    return None


def _ptt_loop():
    """
    Push-to-talk using pynput.keyboard.Listener.

    pynput installs a WH_KEYBOARD_LL hook inside its own dedicated Win32
    message-loop thread, which means it receives every key event system-wide
    including from full-screen DirectX games like ATS/ETS2.  The previous
    GetAsyncKeyState polling approach was silently returning 0 for Delete
    (likely due to the process having no foreground window context).
    """
    log.info("PTT: _ptt_loop thread entered")
    print("[PTT] _ptt_loop thread entered", flush=True)

    try:
        from pynput.keyboard import Listener

        ptt_key = load_prefs().get('push_to_talk_key', PTT_KEY_ENV)
        log.info(f"PTT: configured key={ptt_key!r}")
        print(f"[PTT] key={ptt_key!r}", flush=True)

        target = _resolve_pynput_key(ptt_key)
        log.info(f"PTT: resolved pynput key → {target!r}")
        print(f"[PTT] pynput key={target!r} — hold to record, release to send", flush=True)

        if target is None:
            log.error(f"PTT: cannot resolve pynput key for {ptt_key!r} — PTT disabled")
            print(f"[PTT] ERROR: unknown key {ptt_key!r}", flush=True)
            return

        recording = False
        stop_event: threading.Event | None = None
        record_thread: threading.Thread | None = None
        wav_holder: list = [None]

        def on_press(key):
            nonlocal recording, stop_event, record_thread, wav_holder
            if key == target and not recording:
                recording = True
                stop_event = threading.Event()
                wav_holder = [None]
                log.info("PTT: key DOWN — recording...")
                print("[PTT] KEY DOWN — recording", flush=True)

                # Notify overlay
                try:
                    from overlay import overlay_state
                    overlay_state["recording"] = True
                except Exception:
                    pass

                def do_record(se=stop_event, wh=wav_holder):
                    wh[0] = record_audio(se)

                record_thread = threading.Thread(target=do_record, daemon=True)
                record_thread.start()

        def on_release(key):
            nonlocal recording, stop_event, record_thread, wav_holder
            if key == target and recording:
                recording = False

                # Notify overlay — recording stopped
                try:
                    from overlay import overlay_state
                    overlay_state["recording"] = False
                except Exception:
                    pass

                if stop_event:
                    stop_event.set()
                if record_thread:
                    record_thread.join(timeout=3)

                wav = wav_holder[0]
                if wav:
                    try:
                        size = os.path.getsize(wav)
                        duration = size / (SAMPLE_RATE * 2)
                        log.info(f"PTT: key UP — {duration:.1f}s, {size} bytes — launching pipeline")
                        print(f"[PTT] KEY UP — {duration:.1f}s — sending to pipeline", flush=True)
                    except Exception:
                        log.info("PTT: key UP — wav ready (could not stat)")
                    threading.Thread(
                        target=_process_voice_input, args=(wav,), daemon=True
                    ).start()
                else:
                    log.warning("PTT: key UP but wav=None — too short or mic error")
                    print("[PTT] KEY UP — wav=None (too short or mic error)", flush=True)

        log.info("PTT: starting pynput Listener")
        print("[PTT] pynput Listener starting", flush=True)
        with Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()

    except ImportError:
        log.error("PTT: pynput not installed — run: pip install pynput", exc_info=True)
        print("[PTT] ERROR: pynput not installed", flush=True)
    except Exception as e:
        log.error(f"PTT: _ptt_loop crashed: {e}", exc_info=True)
        print(f"[PTT] CRASHED: {e}", flush=True)


# ── Wake word ─────────────────────────────────────────────────────────────────

def _wake_word_loop():
    """
    Always-on wake word detection via pvporcupine.

    Built-in keywords (free tier): "porcupine", "bumblebee", "hey barista",
    "hey google", "hey siri", "picovoice", "jarvis", "americano", "grasshopper".

    For a custom "Hey Dispatch" wake word: train a model at console.picovoice.ai,
    download the .ppn file, place it in client/, and set WAKE_WORD_KEYWORD_PATH
    in .env to its path.
    """
    if not PICOVOICE_KEY:
        log.warning("PICOVOICE_ACCESS_KEY not set — wake word disabled")
        return

    try:
        import pvporcupine
    except ImportError:
        log.warning("pvporcupine not installed: pip install pvporcupine")
        return

    prefs     = load_prefs()
    keyword   = prefs.get('wake_word_keyword', 'porcupine')
    ppn_path  = os.getenv('WAKE_WORD_KEYWORD_PATH', '')

    try:
        if ppn_path and os.path.exists(ppn_path):
            porcupine = pvporcupine.create(
                access_key=PICOVOICE_KEY,
                keyword_paths=[ppn_path],
                sensitivities=[0.6],
            )
            log.info(f"Wake word: custom model from {ppn_path}")
        else:
            porcupine = pvporcupine.create(
                access_key=PICOVOICE_KEY,
                keywords=[keyword],
            )
            log.info(f"Wake word: built-in keyword '{keyword}'")
    except Exception as e:
        log.error(f"Porcupine init failed: {e}")
        return

    frame_len = porcupine.frame_length
    log.info("Wake word listener active")

    try:
        with sd.InputStream(
            samplerate=porcupine.sample_rate,
            channels=1,
            dtype='int16',
            blocksize=frame_len,
        ) as stream:
            while True:
                frame, _ = stream.read(frame_len)
                result = porcupine.process(frame.flatten())
                if result >= 0:
                    log.info("Wake word detected!")
                    current_prefs = load_prefs()
                    speak("Dispatch, go ahead.", current_prefs)
                    # Pause briefly for acknowledgment to finish, then record
                    time.sleep(0.8)
                    stop_evt = threading.Event()
                    threading.Timer(8.0, stop_evt.set).start()
                    wav = record_audio(stop_evt, max_secs=8)
                    if wav:
                        threading.Thread(
                            target=_process_voice_input, args=(wav,), daemon=True
                        ).start()
    except Exception as e:
        log.error(f"Wake word loop error: {e}")
    finally:
        porcupine.delete()


# ── Proactive briefings ───────────────────────────────────────────────────────

def check_proactive_triggers():
    """
    Called by client.py after each snapshot push.
    Job completion is always announced regardless of cooldown.
    Everything else (fuel warnings, etc.) respects the 3-minute cooldown.
    """
    tel, snap, market, last_brief, start_snap = state.read()
    now = time.monotonic()
    prefs = load_prefs()
    jobs = snap.get('jobs', [])
    job_count = len(jobs)

    # ── Job completion — checked BEFORE cooldown so it always fires ──────────
    if state._prev_job_count < 0:
        # First call: establish baseline without announcing
        state._prev_job_count = job_count
    elif job_count > state._prev_job_count:
        new_count = job_count - state._prev_job_count
        state._prev_job_count = job_count
        log.info(f"Proactive: {new_count} new job(s) detected")
        _brief_job_complete(snap, market, prefs, new_count)
        state.set_briefing_now()
        return

    # ── Everything below respects the cooldown ────────────────────────────────
    if (now - last_brief) < BRIEFING_COOLDOWN:
        return

    # Fuel warning
    fuel = tel.get('fuel_pct', 100.0)
    if 0 < fuel < 20 and not state._fuel_warned:
        state._fuel_warned = True
        msg = f"Fuel alert: you're at {fuel:.0f} percent. Plan a stop."
        log.info("Proactive: fuel warning")
        speak(msg, prefs)
        _append_history('[auto: fuel warning]', msg)
        state.set_briefing_now()
        return
    if fuel >= 20:
        state._fuel_warned = False


def _brief_job_complete(snap: dict, market: list, prefs: dict, new_count: int = 1):
    jobs = snap.get('jobs', [])
    if not jobs:
        return

    last  = jobs[0]
    dest  = last.get('destination_city', '').replace('_', ' ').title()
    rev   = last.get('revenue', 0)
    cargo = last.get('cargo', '').replace('_', ' ')

    # Find which drivers just went idle — best proxy for "who completed"
    drivers = snap.get('drivers', [])
    idle_units = [
        d.get('id', '').replace('driver.', '')
        for d in drivers if d.get('state') != 2 and d.get('id')
    ]

    if new_count == 1 and idle_units:
        completion_line = f"Unit {idle_units[0]} has completed a delivery."
    elif new_count > 1:
        completion_line = f"{new_count} new deliveries logged this session."
    else:
        completion_line = "Delivery complete."

    top = _filter_market(market, prefs)[:3]
    if top:
        opts = '; '.join(
            f"{j['source_city']} to {j['destination_city']} for {_fmt_money(j['revenue'])}"
            for j in top
        )
        msg = (
            f"{completion_line} "
            f"Last haul: {cargo} to {dest}, {_fmt_money(rev)}. "
            f"Top runs available: {opts}."
        )
    else:
        msg = (
            f"{completion_line} "
            f"Last haul: {cargo} to {dest} for {_fmt_money(rev)}. "
            f"Freight market not loaded — sync your save to see options."
        )

    log.info(f"Proactive: job-complete briefing ({new_count} new, unit hint: {idle_units[:1]})")
    speak(msg, prefs)
    _append_history('[auto: job complete]', msg)


def _session_start_briefing():
    """Morning briefing delivered once, 6 seconds after startup (gives snapshot time to load)."""
    global _startup_complete
    time.sleep(6)

    tel, snap, market, _, _ = state.read()
    if not snap:
        log.info("No snapshot yet — skipping session briefing")
        _startup_complete = True   # no briefing to wait for — open PTT gate now
        log.info("Startup gate opened (no snapshot)")
        return

    prefs = load_prefs()
    fin   = snap.get('finances', {})
    drivers = snap.get('drivers', [])
    on_job  = sum(1 for d in drivers if d.get('state') == 2)
    idle    = len(drivers) - on_job
    money   = fin.get('money', 0)
    debt    = fin.get('total_debt', 0)

    top = _filter_market(market, prefs)
    top_str = (
        f"Best available run: {top[0]['source_city']} to {top[0]['destination_city']} "
        f"for {_fmt_money(top[0]['revenue'])}."
        if top else "Freight market not loaded yet."
    )

    msg = (
        f"Dispatch online. Fleet: {len(drivers)} drivers, {on_job} on job, {idle} idle. "
        f"Cash {_fmt_money(money)}, debt {_fmt_money(debt)}. "
        f"{top_str}"
    )

    log.info("Proactive: session start briefing")
    speak(msg, prefs)
    _append_history('[auto: session start]', msg)
    state.set_briefing_now()
    state._session_briefed = True

    # Block until the briefing has fully played, then open the PTT gate.
    # tts_queue.join() returns only after task_done() is called for every
    # item currently in the queue — at startup that's exactly this briefing.
    tts_queue.join()
    _startup_complete = True
    log.info("Startup briefing complete — PTT now active")


# ── Entry point ───────────────────────────────────────────────────────────────

def _ptt_thread_watchdog(ptt_thread: threading.Thread):
    """Periodically log whether the PTT listener thread is still alive."""
    while True:
        time.sleep(15)
        alive = ptt_thread.is_alive()
        log.info(f"PTT watchdog: ptt-listener thread alive={alive}")
        print(f"[PTT] watchdog: thread alive={alive}", flush=True)
        if not alive:
            log.error("PTT: ptt-listener thread has DIED — PTT is non-functional")
            print("[PTT] ERROR: listener thread died — PTT non-functional", flush=True)
            break


def start(voice_mode: str | None = None):
    """
    Start assistant background threads.
    Call from client.py main() after telemetry and watcher are running.
    """
    log.info("assistant.start() called")
    print("[Assistant] start() called", flush=True)

    # TTS worker — single FIFO thread, runs for the lifetime of the process
    threading.Thread(target=_tts_worker, daemon=True, name='tts-worker').start()
    log.info("Assistant: TTS worker thread started")

    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY not set — voice transcription will be unavailable")
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — AI reasoning will be unavailable")

    # Session start briefing (waits 6s for first snapshot)
    threading.Thread(target=_session_start_briefing, daemon=True).start()

    mode = (voice_mode or VOICE_MODE).upper()
    prefs = load_prefs()

    if mode == 'WAKE_WORD' and prefs.get('wake_word_enabled', False):
        threading.Thread(target=_wake_word_loop, daemon=True, name='ww-listener').start()
        log.info("Assistant started in WAKE_WORD mode")
    else:
        ptt_thread = threading.Thread(target=_ptt_loop, daemon=True, name='ptt-listener')
        ptt_thread.start()
        log.info(f"Assistant started in PUSH_TO_TALK mode — key={prefs.get('push_to_talk_key', PTT_KEY_ENV)!r} thread={ptt_thread.ident}")
        print(f"[Assistant] PTT thread started: ident={ptt_thread.ident}", flush=True)
        threading.Thread(target=_ptt_thread_watchdog, args=(ptt_thread,),
                         daemon=True, name='ptt-watchdog').start()
