"""
overlay.py — tkinter HUD for The Dispatch.

Renders an always-on-top transparent window in the top-right corner of the
screen using tkinter + Win32.  Works over ATS/ETS2 in borderless-windowed mode.

Requirements
────────────
• tkinter (stdlib — no extra install needed)
• No admin privileges required.
"""

import ctypes
import threading
import time
import tkinter as tk

# Module-level shared state dict — written by assistant.py, read by the update loop.
overlay_state: dict = {
    "recording":       False,
    "last_transcript": "",
    "transcript_time": 0.0,
    "last_response":   "",
    "response_time":   0.0,
    "current_job":     None,
    "history":         [],
}

_TRANSCRIPT_TTL = 12.0   # seconds to show the transcript after PTT release
_BG    = '#0d0f12'
_AMBER = '#f5a623'
_GREEN = '#1fba5a'
_RED   = '#e53535'
_DIM   = '#6b7280'


def start_overlay(overlay_state):
    print("[Overlay] start_overlay() called", flush=True)

    def _run():
        import traceback
        try:
            print("[Overlay] thread started", flush=True)

            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes('-topmost', True)
            root.attributes('-alpha', 0.88)
            root.configure(bg=_BG)

            sw = root.winfo_screenwidth()
            W, H = 620, 128
            x = sw - W - 10
            root.geometry(f'{W}x{H}+{x}+10')
            print(f"[Overlay] window {W}x{H} at x={x}", flush=True)

            # ── Widgets ──────────────────────────────────────────────────────
            # Row 0: recording indicator | title | mute button
            rec_lbl = tk.Label(root, text="", fg=_RED, bg=_BG,
                               font=('Courier New', 10, 'bold'))
            rec_lbl.place(x=8, y=6)

            title_lbl = tk.Label(root, text="THE DISPATCH",
                                 fg=_AMBER, bg=_BG,
                                 font=('Courier New', 10, 'bold'))
            title_lbl.place(x=220, y=6)

            # Mute toggle — clicking silences TTS
            mute_var = tk.BooleanVar(value=False)
            mute_lbl = tk.Label(root, text="[ MUTE ]",
                                fg=_DIM, bg=_BG,
                                font=('Courier New', 9, 'bold'),
                                cursor='hand2')
            mute_lbl.place(x=512, y=6)

            def _toggle_mute(_event=None):
                muted = not mute_var.get()
                mute_var.set(muted)
                mute_lbl.config(fg=_RED if muted else _DIM,
                                text='[MUTED]' if muted else '[ MUTE ]')
                try:
                    import assistant
                    assistant._set_tts_muted(muted)
                except Exception:
                    pass

            mute_lbl.bind('<Button-1>', _toggle_mute)

            # Row 1: transcript of what the driver said (shown for _TRANSCRIPT_TTL seconds)
            transcript_lbl = tk.Label(root, text="", fg=_GREEN, bg=_BG,
                                      font=('Courier New', 9),
                                      anchor='w', wraplength=596)
            transcript_lbl.place(x=8, y=28)

            # Row 2: current job summary
            job_lbl = tk.Label(root, text="", fg='#f0c040', bg=_BG,
                               font=('Courier New', 9), anchor='w')
            job_lbl.place(x=8, y=54)

            # Row 3: last dispatcher response (up to ~2 lines)
            dispatch_lbl = tk.Label(root, text="", fg='#d0d8e8', bg=_BG,
                                    font=('Courier New', 9),
                                    anchor='w', justify='left',
                                    wraplength=596)
            dispatch_lbl.place(x=8, y=78)

            # Separator line under title row
            sep = tk.Frame(root, bg=_AMBER, height=1)
            sep.place(x=0, y=24, width=W)

            print("[Overlay] widgets created, entering update loop", flush=True)

            # ── Update loop ───────────────────────────────────────────────────
            def update():
                try:
                    root.attributes('-topmost', True)
                    root.lift()

                    # Recording indicator
                    rec_lbl.config(
                        text='● REC' if overlay_state.get('recording') else ''
                    )

                    # Transcript — show briefly then fade
                    txt = overlay_state.get('last_transcript', '')
                    age = time.monotonic() - overlay_state.get('transcript_time', 0)
                    if txt and age < _TRANSCRIPT_TTL:
                        display = f'YOU: {txt[:100]}{"…" if len(txt) > 100 else ""}'
                        transcript_lbl.config(text=display)
                    else:
                        transcript_lbl.config(text='')

                    # Job info
                    job = overlay_state.get('current_job')
                    if job:
                        cargo = job.get('cargo', '—').replace('_', ' ')
                        dest  = job.get('destination', '—')
                        dist  = job.get('distance_miles', 0)
                        job_lbl.config(
                            text=f'JOB  {cargo}  →  {dest}  |  {dist} mi'
                        )
                    else:
                        job_lbl.config(text='No active job')

                    # Dispatcher response
                    resp = overlay_state.get('last_response', '')
                    if resp:
                        # Trim to ~200 chars; wraplength handles line breaks
                        display = resp[:200] + ('…' if len(resp) > 200 else '')
                        dispatch_lbl.config(text=f'DISPATCH  {display}')
                    else:
                        dispatch_lbl.config(text='')

                except Exception:
                    pass

                root.after(500, update)

            update()
            root.mainloop()

        except Exception as e:
            print(f"[Overlay] CRASH: {e}", flush=True)
            traceback.print_exc()

    t = threading.Thread(target=_run, daemon=True, name="overlay-tk")
    t.start()
