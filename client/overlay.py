"""
overlay.py — pygame HUD for The Dispatch.

Renders a transparent always-on-top window in the top-right corner of the
screen.  Works over ATS/ETS2 in borderless-windowed mode.

Requirements
────────────
• pip install pygame
• No admin privileges required.
• Works with borderless-windowed mode (not exclusive fullscreen).
"""

import ctypes
import os
import threading
import time

import pygame

OVERLAY_W = 600
OVERLAY_H = 80

# Module-level shared state dict — written by assistant.py, read by the render loop.
overlay_state: dict = {
    "recording":       False,
    "last_transcript": "",
    "transcript_time": 0.0,
    "last_response":   "",
    "response_time":   0.0,
    "current_job":     None,
    "history":         [],
}


def start_overlay(overlay_state):
    print("[Overlay] start_overlay() called", flush=True)

    def _run():
        import traceback
        try:
            print("[Overlay] thread started", flush=True)

            user32 = ctypes.windll.user32

            # 1. Get screen dimensions and compute position before creating the window
            # SM_CXSCREEN (0) returns the PRIMARY monitor width, but clamp anyway
            # in case of a multi-monitor setup where GetSystemMetrics returns the
            # combined virtual desktop width instead.
            screen_w = user32.GetSystemMetrics(0)
            x = min(screen_w - OVERLAY_W - 10, 1920 - OVERLAY_W - 10)
            y = 10
            print(f"[Overlay] screen_w={screen_w}, placing at x={x}", flush=True)

            # Hint SDL to place the window at the right position on creation
            os.environ['SDL_VIDEO_WINDOW_POS'] = f'{x},{y}'

            pygame.init()
            print("[Overlay] pygame initialized", flush=True)

            screen = pygame.display.set_mode((OVERLAY_W, OVERLAY_H), pygame.NOFRAME)
            pygame.display.set_caption("dispatch-overlay")
            print("[Overlay] window created", flush=True)

            hwnd = pygame.display.get_wm_info()['window']
            print(f"[Overlay] hwnd={hwnd}", flush=True)

            # 2. Set ALL extended styles at once (no read-modify-write — set clean)
            #   WS_EX_LAYERED    — required for colorkey/alpha transparency
            #   WS_EX_TOPMOST    — always on top
            #   WS_EX_NOACTIVATE — never steals or loses focus when other windows are clicked
            #   WS_EX_TOOLWINDOW — hides from taskbar and Alt+Tab
            GWL_EXSTYLE      = -20
            WS_EX_LAYERED    = 0x80000
            WS_EX_TOPMOST    = 0x8
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x80
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )

            # 3. Set colorkey transparency (RGB packed as int: R + G*256 + B*65536)
            colorkey_int = 1 + 1*256 + 1*65536  # RGB(1,1,1)
            ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, colorkey_int, 220, 0x1 | 0x2)

            # 4. Position and force topmost
            HWND_TOPMOST = -1
            user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, OVERLAY_W, OVERLAY_H, 0x0040)  # SWP_SHOWWINDOW
            print(f"[Overlay] positioned at x={x} y={y} ({OVERLAY_W}x{OVERLAY_H}), entering render loop", flush=True)

            # Block QUIT events — the overlay is a daemon thread and dies with
            # the main process; we never want a spurious QUIT (e.g. from ATS
            # stealing focus) to kill the render loop.
            pygame.event.set_blocked(pygame.QUIT)

            font_big = pygame.font.SysFont("Arial", 16, bold=True)
            font_small = pygame.font.SysFont("Arial", 13)
            clock = pygame.time.Clock()

            while True:
                pygame.event.pump()  # keep SDL event queue healthy without processing QUIT

                screen.fill((1, 1, 1))  # colorkey — rendered transparent

                # Draw dark panel
                pygame.draw.rect(screen, (20, 20, 20), (0, 0, OVERLAY_W, OVERLAY_H), border_radius=8)
                pygame.draw.rect(screen, (255, 165, 0), (0, 0, OVERLAY_W, OVERLAY_H), 2, border_radius=8)

                # Recording indicator
                x_off = 10
                if overlay_state.get("recording"):
                    pygame.draw.circle(screen, (255, 0, 0), (x_off + 6, 15), 6)
                    txt = font_big.render("REC", True, (255, 0, 0))
                    screen.blit(txt, (x_off + 16, 6))
                    x_off += 60

                # Job info
                job = overlay_state.get("current_job")
                if job:
                    line1 = f"JOB: {job.get('cargo', '—')} → {job.get('destination', '—')}  |  ${job.get('income', 0):,}  |  {job.get('distance_miles', 0)} mi"
                    txt = font_small.render(line1, True, (255, 220, 100))
                    screen.blit(txt, (10, 30))

                # Last dispatcher response
                last = overlay_state.get("last_response", "")
                if last:
                    display = last[:80] + ("..." if len(last) > 80 else "")
                    txt = font_small.render(f"DISPATCH: {display}", True, (200, 200, 200))
                    screen.blit(txt, (10, 52))

                # Title
                title = font_big.render("THE DISPATCH", True, (255, 165, 0))
                screen.blit(title, (OVERLAY_W - 150, 8))

                pygame.display.flip()

                # 5. Re-assert topmost every frame with position + NOACTIVATE | SHOWWINDOW
                user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, OVERLAY_W, OVERLAY_H,
                                    0x0010 | 0x0040)  # SWP_NOACTIVATE | SWP_SHOWWINDOW

                clock.tick(30)

        except Exception as e:
            print(f"[Overlay] CRASH: {e}", flush=True)
            traceback.print_exc()

    t = threading.Thread(target=_run, daemon=True, name="overlay-pygame")
    t.start()
