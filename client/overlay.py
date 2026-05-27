"""
overlay.py — pygame-based always-on-top HUD for The Dispatch.

Uses SDL/DirectX via pygame, which can render over fullscreen games on Windows.
Windows API SetWindowPos(HWND_TOPMOST) forces z-order above exclusive fullscreen.

NOTE: For most reliable results, run ATS in Borderless Windowed mode.
      DirectX exclusive fullscreen owns the entire display surface; some GPU
      drivers will still prevent any other window from rendering on top of it.
      Borderless Window is a regular desktop window that HWND_TOPMOST can beat.

Thread model:
  - start_overlay() spawns a single daemon thread "overlay-pygame".
  - The thread creates the pygame window, then spins the draw loop at 20 FPS.
  - overlay_state is written by assistant.py and read here — no locking needed
    for simple attribute reads/writes on CPython.
"""

import ctypes
import threading
import time
from dataclasses import dataclass, field

try:
    import pygame
    PYGAME_OK = True
except ImportError:
    PYGAME_OK = False


# ── Shared overlay state ──────────────────────────────────────────────────────
# Written by assistant.py; read by the overlay's draw loop.

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


# ── Layout / palette ──────────────────────────────────────────────────────────

W, H            = 300, 220   # window size in pixels
FPS             = 20
SUBTITLE_SECS   = 8.0
TRANSCRIPT_SECS = 3.5
PAD             = 9

# COLORKEY is painted for "transparent" areas.  SetLayeredWindowAttributes with
# LWA_COLORKEY tells Win32 to composite those pixels as fully transparent.
COLORKEY  = (2, 2, 2)

C_DARK    = (13,  15,  18)
C_AMBER   = (245, 166, 35)
C_TEXT    = (176, 184, 204)
C_DIM     = (90,  100, 118)
C_BRIGHT  = (240, 243, 248)
C_RED     = (255, 80,  80)
C_BORDER  = (60,  65,  75)


# ── Win32 helpers ─────────────────────────────────────────────────────────────

def _screen_size() -> tuple:
    u = ctypes.windll.user32
    return u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def _configure_window(hwnd: int, x: int, y: int, w: int, h: int):
    """
    1. SetWindowPos(HWND_TOPMOST) — forces the window above all others including
       exclusive-fullscreen DirectX apps.
    2. SetLayeredWindowAttributes — combines:
         LWA_COLORKEY  →  pixels painted COLORKEY become fully transparent
         LWA_ALPHA     →  remaining pixels rendered at 88 % opacity (dark glass)
    """
    user32 = ctypes.windll.user32

    HWND_TOPMOST   = -1
    SWP_SHOWWINDOW = 0x0040
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_SHOWWINDOW)

    GWL_EXSTYLE    = -20
    WS_EX_LAYERED  = 0x00080000
    LWA_COLORKEY   = 0x00000001
    LWA_ALPHA      = 0x00000002
    WINDOW_ALPHA   = 225   # 0-255; 225 ≈ 88 % opaque — dark glass feel

    r, g, b  = COLORKEY
    colorref = r | (g << 8) | (b << 16)

    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
    user32.SetLayeredWindowAttributes(hwnd, colorref, WINDOW_ALPHA,
                                      LWA_COLORKEY | LWA_ALPHA)


# ── Text utilities ────────────────────────────────────────────────────────────

def _blit(surf, font, text: str, color, xy: tuple, max_w: int = 9999):
    """Render one line, truncating with '…' if wider than max_w. Returns height."""
    if font.size(text)[0] > max_w:
        while text and font.size(text + '…')[0] > max_w:
            text = text[:-1]
        text += '…'
    s = font.render(text, True, color)
    surf.blit(s, xy)
    return s.get_height()


def _wrap(text: str, font, max_w: int) -> list:
    """Word-wrap text into lines no wider than max_w pixels."""
    words, lines, cur = text.split(), [], []
    for word in words:
        probe = ' '.join(cur + [word])
        if font.size(probe)[0] <= max_w:
            cur.append(word)
        else:
            if cur:
                lines.append(' '.join(cur))
            cur = [word]
    if cur:
        lines.append(' '.join(cur))
    return lines or ['']


# ── Main render loop ──────────────────────────────────────────────────────────

def _run_overlay():
    if not PYGAME_OK:
        print("[Overlay] pygame not installed — overlay disabled.  pip install pygame",
              flush=True)
        return

    print("[Overlay] Starting pygame overlay …", flush=True)
    print(
        "[Overlay] NOTE: run ATS in Borderless Windowed mode for the overlay to appear.\n"
        "          Options → Graphics → Display Mode → Borderless Window",
        flush=True,
    )

    try:
        pygame.init()
        pygame.font.init()
    except Exception as exc:
        print(f"[Overlay] pygame.init() failed: {exc}", flush=True)
        return

    sw, sh = _screen_size()
    ox, oy = sw - W - 12, 12   # top-right corner, 12 px from edges

    try:
        screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
        pygame.display.set_caption("Dispatch HUD")
    except Exception as exc:
        print(f"[Overlay] Display init failed: {exc}", flush=True)
        return

    # Win32: force topmost + set up transparent colorkey + window alpha
    try:
        hwnd = pygame.display.get_wm_info()['window']
        _configure_window(hwnd, ox, oy, W, H)
        print(f"[Overlay] Window at ({ox},{oy}), HWND_TOPMOST + layered attrs set",
              flush=True)
    except Exception as exc:
        print(f"[Overlay] Win32 setup failed: {exc}", flush=True)

    # Fonts
    try:
        f_sm  = pygame.font.SysFont("Consolas", 10)
        f_med = pygame.font.SysFont("Consolas", 11)
        f_hdr = pygame.font.SysFont("Consolas", 11, bold=True)
    except Exception:
        f_sm = f_med = f_hdr = pygame.font.Font(None, 14)

    clock     = pygame.time.Clock()
    rec_tick  = 0

    while True:
        # Pump events — required to prevent Windows marking the process as
        # "not responding" and to receive WM_QUIT if the window is destroyed.
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return

        ov  = overlay_state
        now = time.monotonic()

        # ── Background ───────────────────────────────────────────────────────
        # Fill with COLORKEY first (all pixels = transparent via Win32 colorkey).
        screen.fill(COLORKEY)
        # Draw rounded dark panel — corners stay COLORKEY → transparent.
        pygame.draw.rect(screen, C_DARK, pygame.Rect(0, 0, W, H), border_radius=8)
        # Double amber top border
        pygame.draw.line(screen, C_AMBER, (8, 1), (W - 8, 1), 2)

        y = 6

        # ── Header ───────────────────────────────────────────────────────────
        rec_tick = (rec_tick + 1) % FPS
        if ov.recording:
            dot_txt = "● REC"
            dot_col = C_RED
        else:
            dot_txt = "●"
            dot_col = C_DIM

        _blit(screen, f_hdr, dot_txt, dot_col, (PAD, y))
        title_s = f_hdr.render("DISPATCH", True, C_AMBER)
        screen.blit(title_s, (W // 2 - title_s.get_width() // 2, y))
        y += 18

        pygame.draw.line(screen, C_BORDER, (0, y), (W, y), 1)
        y += 5

        # ── Current job ───────────────────────────────────────────────────────
        try:
            import assistant as _a
            tel, snap, _, _, _ = _a.state.read()
            player     = snap.get("player", {}) or {}
            in_job     = player.get("in_job", False)
            live_cargo = (tel.get("cargo") or "").strip()
            live_mass  = float(tel.get("cargo_mass") or 0)

            if in_job or live_mass > 0 or live_cargo:
                cargo   = live_cargo or player.get("job_info_cargo") or "Unknown cargo"
                target  = player.get("job_info_target") or ""
                dist_km = int(player.get("job_info_planned_distance_km") or 0)
                dist_mi = round(dist_km * 0.621371)
                parts   = target.split(".") if target else []
                dest    = parts[-1].replace("_", " ").title() if parts else "Unknown"

                _blit(screen, f_sm,  "CURRENT JOB",       C_AMBER,  (PAD, y))
                y += 13
                _blit(screen, f_med, cargo,                C_BRIGHT, (PAD, y), W - PAD * 2)
                y += 14
                _blit(screen, f_sm,  f"→ {dest}   {dist_mi} mi",
                      C_TEXT,  (PAD, y))
                y += 15
            else:
                _blit(screen, f_sm, "No active load", C_DIM, (PAD, y))
                y += 15

        except Exception:
            _blit(screen, f_sm, "Waiting for game data…", C_DIM, (PAD, y))
            y += 15

        pygame.draw.line(screen, C_BORDER, (PAD, y), (W - PAD, y), 1)
        y += 5

        # ── AI subtitle — fades over SUBTITLE_SECS ────────────────────────────
        if ov.last_response and (now - ov.response_time) < SUBTITLE_SECS:
            age   = now - ov.response_time
            ratio = 1.0 - age / SUBTITLE_SECS
            alpha = max(55, int(255 * ratio))
            col   = tuple(int(c * alpha // 255) for c in C_BRIGHT)
            acol  = tuple(int(c * alpha // 255) for c in C_AMBER)

            # Amber left-edge accent bar
            bar_h = min(3 * 13 + 4, H - y - 4)
            if bar_h > 0:
                pygame.draw.line(screen, acol, (PAD, y), (PAD, y + bar_h), 2)

            lines = _wrap(ov.last_response, f_sm, W - PAD * 2 - 8)
            for line in lines[:3]:
                _blit(screen, f_sm, line, col, (PAD + 6, y))
                y += 13

        elif ov.last_transcript and (now - ov.transcript_time) < TRANSCRIPT_SECS:
            # Transcript echo (italic-style: dim colour)
            _blit(screen, f_sm,
                  f"You: {ov.last_transcript}",
                  C_DIM, (PAD, y), W - PAD * 2)

        pygame.display.flip()
        clock.tick(FPS)


# ── Entry point ───────────────────────────────────────────────────────────────

def start_overlay():
    """
    Launch the pygame overlay in a daemon thread.  Returns the thread object.
    The overlay renders independently; the caller does not need to run an
    event loop — pygame manages its own SDL event pump inside _run_overlay().
    """
    if not PYGAME_OK:
        print(
            "[Overlay] pygame not installed — overlay disabled.\n"
            "          Install it with:  pip install pygame",
            flush=True,
        )
        return None

    t = threading.Thread(target=_run_overlay, daemon=True, name="overlay-pygame")
    t.start()
    return t
