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
    "You are a gruff truck dispatcher on CB radio. Natural spoken voice only — no symbols, "
    "no arrows, no formatting.\n\n"
    "For job listings, keep it tight. One short sentence per job: cargo, destination, pay. "
    "Max 3 jobs. Example: \"Best paying right now is home accessories to Fredericton, two "
    "hundred and five thousand dollars. Got a Darkwing run to Rd1, one ninety thousand. "
    "Sand to Jacksonville, one eighty-six thousand.\"\n\n"
    "You will be given a list of jobs. Each job includes which market it came from. Group "
    "jobs by market when listing them. Say which market they're from: 'cargo market', "
    "'freight market', or 'quick jobs'. Example: 'Best three are out of the cargo market "
    "— home accessories to Fredericton, two hundred and five thousand dollars. Darkwing to "
    "Rd1...' If all from the same market, just say it once upfront.\n\n"
    "For all other questions: 1-2 sentences max.\n\n"
    "Distances are in miles — say \"miles\" not \"klicks\". Use trucker lingo naturally. "
    "Always say the full dollar amount out loud. Say \"two hundred and five thousand dollars\" "
    "not \"two oh five thousand\". Never abbreviate pay amounts. "
    "Stop talking the moment the info is delivered.\n\n"
    "You will be told if the driver already has an active load. If they do, do not list new "
    "jobs — instead confirm their current delivery details if asked. Only list available jobs "
    "when the driver has no active load or explicitly asks for options."
)

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


def build_context(telemetry: dict, snapshot: dict, freight_market: list,
                  prefs: dict, session_start_snap: dict) -> str:
    """Build a compact context string (~800-1200 tokens) for Claude's system prompt."""
    lines = []

    if telemetry:
        spd  = telemetry.get('speed_kmh', 0)
        fuel = telemetry.get('fuel_pct', 0)
        gear = telemetry.get('gear', 0)
        cargo = telemetry.get('cargo', '')
        truck = f"{telemetry.get('truck_make', '')} {telemetry.get('truck_model', '')}".strip()
        line  = f"LIVE: {spd:.0f} km/h | Gear {gear} | Fuel {fuel:.0f}%"
        if cargo:
            line += f" | Cargo: {cargo}"
        if truck:
            line += f" | Truck: {truck}"
        lines.append(line)

    fin = snapshot.get('finances', {})
    if fin:
        money = fin.get('money', 0)
        debt  = fin.get('total_debt', 0)
        net   = money - debt
        lines.append(
            f"FINANCES: Cash {_fmt_money(money)} | Debt {_fmt_money(debt)} | Net {_fmt_money(net)}"
        )

    drivers = snapshot.get('drivers', [])
    if drivers:
        on_job = [d for d in drivers if d.get('state') == 2]
        idle   = [d for d in drivers if d.get('state') != 2]
        lines.append(f"FLEET: {len(drivers)} drivers | {len(on_job)} on job | {len(idle)} idle")
        if idle:
            idle_info = [
                f"Unit {d.get('id','').replace('driver.','')}"
                f" in {d.get('current_city','').replace('_',' ').title()}"
                for d in idle[:4]
            ]
            lines.append("IDLE: " + ", ".join(idle_info))

    jobs = snapshot.get('jobs', [])
    if jobs:
        total_rev_5 = sum(j.get('revenue', 0) for j in jobs[:5])
        lines.append(f"RECENT (last 5 jobs): {_fmt_money(total_rev_5)} earned")
        for j in jobs[:3]:
            src  = j.get('source_city', '').replace('_', ' ').title()
            dst  = j.get('destination_city', '').replace('_', ' ').title()
            cargo = j.get('cargo', '').replace('_', ' ')
            lines.append(f"  {src} → {dst} | {_fmt_money(j.get('revenue',0))} | {cargo}")

    # Session delta
    if session_start_snap and snapshot:
        n0 = len(session_start_snap.get('jobs', []))
        n1 = len(snapshot.get('jobs', []))
        if n1 > n0:
            sess_rev = sum(j.get('revenue', 0) for j in snapshot['jobs'][:n1 - n0])
            lines.append(f"THIS SESSION: {n1 - n0} jobs | {_fmt_money(sess_rev)} earned")

    # Freight market (top filtered jobs)
    if freight_market:
        filtered = _filter_market(freight_market, prefs)
        lines.append(f"FREIGHT MARKET ({len(freight_market)} available, top matches):")
        for j in filtered[:5]:
            src  = j.get('source_city', '')
            dst  = j.get('destination_city', '')
            rev  = j.get('revenue', 0)
            dist = j.get('distance_km', 0)
            cargo = j.get('cargo', '')
            lines.append(f"  {src} → {dst} | {_fmt_money(rev)} | {dist} km | {cargo}")
    elif not freight_market:
        lines.append("FREIGHT MARKET: No market data — sync save file to populate.")

    # Preferences summary
    pref_parts = []
    if prefs.get('preferred_cargo'):
        pref_parts.append(f"preferred cargo: {', '.join(prefs['preferred_cargo'])}")
    if prefs.get('avoided_cargo'):
        pref_parts.append(f"avoid: {', '.join(prefs['avoided_cargo'])}")
    if prefs.get('preferred_min_revenue'):
        pref_parts.append(f"min revenue: {_fmt_money(prefs['preferred_min_revenue'])}")
    if prefs.get('notes'):
        pref_parts.append(f"notes: {prefs['notes']}")
    if pref_parts:
        lines.append("PREFERENCES: " + " | ".join(pref_parts))

    # In-game time
    player = snapshot.get('player', {})
    gm = player.get('game_time_minutes', 0)
    if gm:
        days  = gm // (60 * 24)
        hours = (gm % (60 * 24)) // 60
        mins  = gm % 60
        lines.append(f"IN-GAME: Day {days}, {hours:02d}:{mins:02d}")

    return '\n'.join(lines)


# ── Snapshot summariser ───────────────────────────────────────────────────────

def build_context_summary(snapshot: dict) -> str:
    if not snapshot:
        return "No game data loaded."

    ctx: dict = {}
    ctx["money"] = snapshot.get("finances", {}).get("money", "unknown")

    # ── Trailer extraction ────────────────────────────────────────────────────
    trailer = snapshot.get("trailer", {}) or {}
    log.info(f"SNAP_TRAILER: {trailer!r}")

    active_cargo       = trailer.get("active_cargo", "") or ""
    active_destination = trailer.get("active_destination", "") or ""

    # Extract cargo type code: 'cargo.catd6rpm' → 'catd6rpm'
    trailer_cargo_type = ""
    if active_cargo:
        parts = active_cargo.split(".", 1)
        trailer_cargo_type = parts[1] if len(parts) > 1 else active_cargo

    trailer_attached = bool(trailer)
    has_active_job   = bool(active_destination)

    ctx["trailer_attached"]   = trailer_attached
    ctx["trailer_cargo_type"] = trailer_cargo_type or None
    ctx["has_active_job"]     = has_active_job

    log.info(
        f"TRAILER STATE — attached={trailer_attached}, "
        f"cargo_type={trailer_cargo_type!r}, has_active_job={has_active_job}"
    )

    # ── Raw job / market data ─────────────────────────────────────────────────
    offers = snapshot.get("freight_market", [])
    if isinstance(offers, dict):
        offers = offers.get("offers", [])
    if not isinstance(offers, list):
        offers = []

    jobs = snapshot.get("jobs", [])
    if not isinstance(jobs, list):
        jobs = []

    log.info(f"SNAP_JOBS_RAW[:3]: {jobs[:3]!r}")
    log.info(f"SNAP_FM_RAW[:1]: {offers[:1]!r}")

    # ── Job source logic ──────────────────────────────────────────────────────
    if has_active_job:
        # Driver is already loaded — surface the active run, suppress new job listings
        dest_parts = active_destination.split(".")
        dest_city  = dest_parts[-1].replace("_", " ").title() if dest_parts else active_destination
        company    = dest_parts[1].replace("_", " ").title() if len(dest_parts) > 1 else ""

        ctx["current_job"] = {
            "cargo":           trailer_cargo_type or active_cargo,
            "destination":     dest_city,
            "company":         company,
            "raw_destination": active_destination,
        }
        ctx["job_status"] = (
            "ACTIVE_LOAD: Driver already has an active load. "
            "Do not suggest new jobs unless explicitly asked."
        )
        candidates = []
        log.info(
            f"Job source: active load in progress → "
            f"dest={dest_city!r}, cargo_type={trailer_cargo_type!r}"
        )

    elif trailer_attached and trailer_cargo_type:
        # Trailer attached, no active job — filter available jobs to matching cargo type
        ctx["current_job"] = None
        matched = [
            j for j in jobs
            if trailer_cargo_type.lower() in j.get("cargo", "").lower()
        ]
        if matched:
            candidates = matched
            log.info(
                f"Job source: trailer-filtered jobs "
                f"(type={trailer_cargo_type!r}, {len(matched)} matches of {len(jobs)})"
            )
        else:
            candidates = jobs
            log.info(
                f"Job source: trailer attached (type={trailer_cargo_type!r}) but no cargo "
                f"match — showing all {len(jobs)} jobs"
            )

    else:
        # No trailer — show freight_market (company trailer) jobs
        ctx["current_job"] = None
        candidates = offers if offers else jobs
        log.info(
            f"Job source: no trailer → freight_market ({len(offers)} offers) "
            f"or jobs fallback ({len(jobs)})"
        )

    # ── Shared fields ─────────────────────────────────────────────────────────
    ctx["current_city"] = (
        snapshot.get("current_city")
        or snapshot.get("city")
        or (candidates[0].get("source_city") if candidates else None)
        or "unknown"
    )

    top_jobs = sorted(
        [j for j in candidates if isinstance(j, dict)],
        key=lambda x: x.get("revenue", 0),
        reverse=True,
    )[:10]

    ctx["units"] = "miles, USD"
    ctx["top_freight_jobs"] = [
        {
            "cargo":       j.get("cargo", "unknown"),
            "source":      j.get("source_city", "?"),
            "destination": j.get("destination_city", "?"),
            "income":      j.get("revenue", 0),
            "distance":    round(int(j.get("distance_km", 0)) * 0.621371),
            "market":      j.get("market", "unknown"),
        }
        for j in top_jobs
    ]

    ctx["drivers"] = [
        {"name": d.get("name"), "city": d.get("city"), "status": d.get("status")}
        for d in snapshot.get("drivers", [])[:5]
    ]

    return json.dumps(ctx, indent=2)


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

    personality = prefs.get('personality', 'professional')

    # ── Compact snapshot summary (replaces raw JSON dump) ─────────────────────
    summary = build_context_summary(snapshot)
    full_message = f"Current game data:\n{summary}\n\n{user_message}"

    est_tokens = len(full_message) // 4
    log.info(f"Claude: user_message={user_message!r}")
    log.info(f"Claude: full_message ~{est_tokens} tokens ({len(full_message)} chars)")

    # ── Build messages array with capped history ──────────────────────────────
    with _conv_lock:
        recent = list(_conv_history[-6:])   # last 6 messages (3 turns)

    messages_payload = recent + [{'role': 'user', 'content': full_message}]

    try:
        log.info(f"Claude: sending request (model=claude-sonnet-4-5, personality={personality}, "
                 f"history_msgs={len(recent)})")
        resp = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=120,
            system=SYSTEM_PROMPT,
            messages=messages_payload,
        )
        log.info(f"Claude: raw response content={resp.content!r}")
        text = resp.content[0].text.strip()
        log.info(f"Claude: received {len(text)} chars, stop_reason={resp.stop_reason}")

        # Persist this turn to in-memory history
        with _conv_lock:
            _conv_history.append({'role': 'user', 'content': full_message})
            _conv_history.append({'role': 'assistant', 'content': text})
            _conv_history = _conv_history[-6:]   # keep at most 6 messages

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


def speak(text: str, prefs: dict | None = None):
    """Enqueue text for TTS — never interrupts current speech."""
    tts_queue.put(text)


def _tts_worker():
    """Single dedicated daemon thread: speaks one queued item at a time, FIFO."""
    while True:
        text = tts_queue.get()
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

                def do_record(se=stop_event, wh=wav_holder):
                    wh[0] = record_audio(se)

                record_thread = threading.Thread(target=do_record, daemon=True)
                record_thread.start()

        def on_release(key):
            nonlocal recording, stop_event, record_thread, wav_holder
            if key == target and recording:
                recording = False
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
    time.sleep(6)

    tel, snap, market, _, _ = state.read()
    if not snap:
        log.info("No snapshot yet — skipping session briefing")
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
