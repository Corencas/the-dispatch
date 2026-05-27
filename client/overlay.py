"""
overlay.py — game-overlay-sdk HUD for The Dispatch.

Injects a DLL into the ATS/ETS2 DirectX process and renders text directly
inside the game's frame buffer — works over true exclusive fullscreen.

Requirements / caveats
──────────────────────
• pip install game-overlay-sdk
• Client MUST be run as Administrator (DLL injection requires elevated privileges).
• start_monitor() listens for NEW process-creation events — it will not inject
  into a game that is already running.  Launch order must be:
    1. Start The Dispatch client (as Administrator)
    2. Launch ATS / ETS2
• Steam forks game processes by default, breaking injection.  Fix: create a
  file called steam_appid.txt containing the app ID in the game's install
  directory (ATS: 270880 / ETS2: 227300).  One-time setup.
• Vulkan SDK must be installed (the injected DLL requires it).
• Only the latest send_message() call is displayed per frame; updates are
  capped at ~200 ms by the injected DLL.
"""

import logging
import threading
import time
from dataclasses import dataclass, field

try:
    import game_overlay_sdk.injector as injector
    OVERLAY_SDK_AVAILABLE = True
except Exception as e:
    OVERLAY_SDK_AVAILABLE = False
    print(f"[Overlay] game-overlay-sdk import failed: {e}")
    print(f"[Overlay] overlay disabled.")

log = logging.getLogger('overlay')

# ── Shared overlay state ──────────────────────────────────────────────────────
# Written by assistant.py; read here in the message-build loop.

@dataclass
class OverlayState:
    recording: bool        = False
    muted: bool            = False
    last_transcript: str   = ""
    transcript_time: float = 0.0    # time.monotonic()
    last_response: str     = ""
    response_time: float   = 0.0    # time.monotonic()
    history: list          = field(default_factory=list)

overlay_state = OverlayState()

# ── Config ────────────────────────────────────────────────────────────────────

ATS_EXE       = "amtrucks.exe"
ETS2_EXE      = "eurotrucks2.exe"
SUBTITLE_SECS = 8.0
SEND_INTERVAL = 0.5   # seconds between send_message() calls


# ── Message builder ───────────────────────────────────────────────────────────

def _build_message(ov: OverlayState) -> str:
    """
    Compose the overlay text from current overlay + game state.
    All content fits on a single line separated by  |  so the SDK can
    render it as one text block inside the game frame.
    """
    parts = []
    now   = time.monotonic()

    # Recording indicator
    if ov.recording:
        parts.append("[ REC ]")

    # Mute indicator
    if ov.muted:
        parts.append("[ MUTED ]")

    # Current job — read live from assistant state (authoritative source)
    try:
        import assistant as _a
        tel, snap, _, _, _ = _a.state.read()
        player     = snap.get("player", {}) or {}
        in_job     = player.get("in_job", False)
        live_cargo = (tel.get("cargo") or "").strip()
        live_mass  = float(tel.get("cargo_mass") or 0)

        if in_job or live_mass > 0 or live_cargo:
            cargo   = live_cargo or player.get("job_info_cargo") or "?"
            target  = player.get("job_info_target") or ""
            dist_km = int(player.get("job_info_planned_distance_km") or 0)
            dist_mi = round(dist_km * 0.621371)
            t_parts = target.split(".") if target else []
            dest    = t_parts[-1].replace("_", " ").title() if t_parts else "?"
            parts.append(f"JOB: {cargo} -> {dest}")
            if dist_mi:
                parts.append(f"{dist_mi} mi")
        else:
            parts.append("No active load")

        # Fuel warning
        fuel = float(tel.get("fuel_pct") or 100)
        if 0 < fuel < 20:
            parts.append(f"!! FUEL: {fuel:.0f}%")

    except Exception:
        parts.append("DISPATCH READY")

    # AI subtitle — show for SUBTITLE_SECS then drop it
    if ov.last_response and (now - ov.response_time) < SUBTITLE_SECS:
        resp = ov.last_response
        if len(resp) > 100:
            resp = resp[:97] + "..."
        parts.append(f"DISPATCH: {resp}")

    return "  |  ".join(parts) if parts else "DISPATCH READY"


# ── Overlay thread ────────────────────────────────────────────────────────────

def _run(ov: OverlayState):
    """
    Daemon thread: calls injector.start_monitor(), waits for injection,
    then loops sending HUD updates every SEND_INTERVAL seconds.

    InjectionError handling mirrors the official SDK example:
      TARGET_PROCESS_IS_NOT_CREATED_ERROR  →  game not launched yet, wait and retry
      TARGET_PROCESS_WAS_TERMINATED_ERROR  →  game exited, stop the thread
    """
    if not OVERLAY_SDK_AVAILABLE:
        print(
            "[Overlay] game-overlay-sdk not installed — overlay disabled.\n"
            "          pip install game-overlay-sdk",
            flush=True,
        )
        return

    print(
        f"[Overlay] Monitoring for {ATS_EXE} / {ETS2_EXE} …\n"
        "[Overlay] IMPORTANT: client must run as Administrator for injection.\n"
        "[Overlay] Launch ATS/ETS2 AFTER starting this client so the monitor\n"
        "          can catch the process-creation event.",
        flush=True,
    )

    try:
        injector.enable_monitor_logger()
        injector.start_monitor(ATS_EXE)
        log.info(f"[Overlay] start_monitor({ATS_EXE!r}) registered")
    except Exception as exc:
        log.error(f"[Overlay] start_monitor failed: {exc}")
        print(f"[Overlay] start_monitor failed: {exc}", flush=True)
        return

    # Give the injection a moment to complete after process creation
    time.sleep(3)
    log.info("[Overlay] Injection wait done — entering message loop")
    print("[Overlay] Injection wait done — message loop active", flush=True)

    while True:
        try:
            msg = _build_message(ov)
            injector.send_message(msg)

        except injector.InjectionError as err:
            ec            = err.exit_code
            not_created   = injector.CustomExitCodes.TARGET_PROCESS_IS_NOT_CREATED_ERROR.value
            terminated    = injector.CustomExitCodes.TARGET_PROCESS_WAS_TERMINATED_ERROR.value

            if ec == not_created:
                # Game hasn't launched yet — poll quietly
                log.debug("[Overlay] waiting for game process …")
                time.sleep(5)
                continue
            elif ec == terminated:
                log.warning("[Overlay] game process terminated — overlay thread exiting")
                print("[Overlay] Game process terminated — overlay stopped", flush=True)
                try:
                    injector.release_resources()
                except Exception:
                    pass
                return
            else:
                log.warning(f"[Overlay] InjectionError exit_code={ec} — retrying")

        except Exception as exc:
            log.debug(f"[Overlay] send_message error: {exc}")

        time.sleep(SEND_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

def start_overlay(ov: OverlayState = None):
    """
    Inject into ATS and start sending HUD updates.

    Parameters
    ----------
    ov : OverlayState, optional
        Shared state written by assistant.py (recording flag, last AI response,
        etc.).  Defaults to the module-level overlay_state instance.

    Returns the daemon thread so the caller can keep a reference.

    Requires:
      - game-overlay-sdk installed  (pip install game-overlay-sdk)
      - Process running as Administrator
      - ATS launched AFTER this function is called
    """
    if not OVERLAY_SDK_AVAILABLE:
        print(
            "[Overlay] game-overlay-sdk not installed — overlay disabled.\n"
            "          pip install game-overlay-sdk",
            flush=True,
        )
        return None

    target = ov if ov is not None else overlay_state
    t = threading.Thread(target=_run, args=(target,), daemon=True, name="overlay-sdk")
    t.start()
    return t
