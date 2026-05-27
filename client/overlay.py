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
import threading
import time

import pygame

OVERLAY_W = 600
OVERLAY_H = 80


def start_overlay(overlay_state):
    def _run():
        pygame.init()
        screen = pygame.display.set_mode((OVERLAY_W, OVERLAY_H), pygame.NOFRAME)
        pygame.display.set_caption("dispatch-overlay")

        # Force window to top-right of screen, always on top
        hwnd = pygame.display.get_wm_info()['window']
        user32 = ctypes.windll.user32
        screen_w = user32.GetSystemMetrics(0)

        # Position top-right
        x = screen_w - OVERLAY_W - 10
        y = 10

        SWP_SHOWWINDOW = 0x0040
        HWND_TOPMOST = -1
        user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, OVERLAY_W, OVERLAY_H, SWP_SHOWWINDOW)

        # Set layered window style before applying attributes
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x80000
        WS_EX_TOPMOST = 0x8
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TOPMOST)

        # Make window background transparent using colorkey
        colorkey = (1, 1, 1)
        ctypes.windll.user32.SetLayeredWindowAttributes(
            hwnd,
            ctypes.windll.gdi32.RGB(1, 1, 1),
            200,  # alpha 0-255
            0x00000001 | 0x00000002  # LWA_COLORKEY | LWA_ALPHA
        )

        font_big = pygame.font.SysFont("Arial", 16, bold=True)
        font_small = pygame.font.SysFont("Arial", 13)
        clock = pygame.time.Clock()

        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return

            screen.fill(colorkey)  # transparent background

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

            # Re-assert topmost every frame
            user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, OVERLAY_W, OVERLAY_H, SWP_SHOWWINDOW)

            clock.tick(30)

    t = threading.Thread(target=_run, daemon=True, name="overlay-pygame")
    t.start()
