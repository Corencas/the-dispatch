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


def start_overlay(overlay_state):
    print("[Overlay] start_overlay() called", flush=True)

    def _run():
        import traceback
        try:
            print("[Overlay] thread started", flush=True)

            root = tk.Tk()
            root.overrideredirect(True)       # no title bar / border
            root.attributes('-topmost', True)
            root.attributes('-alpha', 0.85)
            root.configure(bg='black')

            sw = root.winfo_screenwidth()
            x  = sw - 610
            root.geometry(f'600x80+{x}+10')
            print(f"[Overlay] window created at x={x} y=10", flush=True)

            # Force HWND_TOPMOST via Win32 as well
            hwnd = ctypes.windll.user32.FindWindowW(None, "")
            HWND_TOPMOST = -1
            SWP_FLAGS = 0x0010 | 0x0001 | 0x0002  # NOACTIVATE | NOSIZE | NOMOVE
            ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
            print(f"[Overlay] hwnd={hwnd}, HWND_TOPMOST set", flush=True)

            # ── Widgets ──────────────────────────────────────────────────────
            title_lbl = tk.Label(root, text="THE DISPATCH",
                                 fg='orange', bg='black', font=('Arial', 11, 'bold'))
            title_lbl.place(x=450, y=5)

            rec_lbl = tk.Label(root, text="",
                               fg='red', bg='black', font=('Arial', 10, 'bold'))
            rec_lbl.place(x=10, y=5)

            job_lbl = tk.Label(root, text="",
                               fg='yellow', bg='black', font=('Arial', 10))
            job_lbl.place(x=10, y=28)

            dispatch_lbl = tk.Label(root, text="",
                                    fg='white', bg='black', font=('Arial', 9))
            dispatch_lbl.place(x=10, y=52)

            print("[Overlay] entering update loop", flush=True)

            # ── Update loop (runs on the Tk thread via after()) ───────────────
            def update():
                try:
                    # Re-assert topmost every tick
                    root.attributes('-topmost', True)
                    root.lift()

                    # Recording indicator
                    rec_lbl.config(text="● REC" if overlay_state.get("recording") else "")

                    # Job info
                    job = overlay_state.get("current_job")
                    if job:
                        job_lbl.config(
                            text=f"JOB: {job.get('cargo', '—')} → {job.get('destination', '—')}"
                                 f"  |  ${job.get('income', 0):,}  |  {job.get('distance_miles', 0)} mi"
                        )
                    else:
                        job_lbl.config(text="No active job")

                    # Last dispatcher response
                    last = overlay_state.get("last_response", "")
                    if last:
                        display = last[:80] + ("..." if len(last) > 80 else "")
                        dispatch_lbl.config(text=f"DISPATCH: {display}")
                    else:
                        dispatch_lbl.config(text="")

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
