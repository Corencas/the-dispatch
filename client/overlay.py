"""
overlay.py — Transparent always-on-top HUD for The Dispatch.

Created in the main thread by client.py after QApplication is initialised.
Qt requires that QApplication and all widgets live on the main thread;
the event loop (app.exec()) is also run there.

Behaviour:
  - Fully click-through by default (WS_EX_TRANSPARENT) so ATS/ETS2
    receives all keyboard and mouse input uninterrupted.
  - Hovering over the overlay removes click-through so the header
    controls (collapse / mute) become interactive.
  - Drag anywhere on the window to reposition.
  - Press the collapse button (━) to shrink to just the header bar.

Shared state is written by assistant.py via the overlay_state object
and read here via a 500 ms QTimer.
"""

import time
from dataclasses import dataclass, field

try:
    from PyQt6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QFrame, QSizePolicy,
    )
    from PyQt6.QtCore import Qt, QTimer, QPoint
    from PyQt6.QtGui import QPainter, QColor, QPen, QBrush
    PYQT6_OK = True
except ImportError:
    PYQT6_OK = False


# ── Shared overlay state ──────────────────────────────────────────────────────
# Written by assistant.py; read by the overlay's refresh timer.

@dataclass
class OverlayState:
    recording: bool       = False
    muted: bool           = False
    last_transcript: str  = ""
    transcript_time: float = 0.0    # time.monotonic()
    last_response: str    = ""
    response_time: float  = 0.0     # time.monotonic()
    # Clean conversation history: list of {"role": "user"|"assistant", "text": str}
    history: list         = field(default_factory=list)

overlay_state = OverlayState()


# ── Palette ───────────────────────────────────────────────────────────────────

_C = {
    "bg":       QColor(13, 15, 18, 215),
    "panel":    QColor(20, 24, 30, 160),
    "border":   QColor(245, 166, 35, 70),
    "amber":    QColor(245, 166, 35),
    "amber_lo": QColor(245, 166, 35, 100),
    "text":     QColor(176, 184, 204),
    "bright":   QColor(240, 243, 248),
    "dim":      QColor(90, 100, 118),
    "red":      QColor(255, 80, 80),
    "green":    QColor(31, 186, 90),
}

_SUBTITLE_SECS   = 9.0   # fade window for AI response subtitle
_TRANSCRIPT_SECS = 3.5   # fade window for your transcript
_WIDTH           = 295


def _css_col(key):
    c = _C[key]
    return f"rgb({c.red()},{c.green()},{c.blue()})"


def _base_qss():
    a, t, d, bg = _css_col("amber"), _css_col("text"), _css_col("dim"), "transparent"
    return f"""
QWidget  {{ background: {bg}; color: {t}; }}
QLabel   {{ background: {bg}; color: {t}; font-family: Consolas, "Courier New"; font-size: 9px; }}
QPushButton {{
    background: rgba(245,166,35,35);
    color: {a};
    border: 1px solid rgba(245,166,35,70);
    border-radius: 3px;
    font-family: Consolas, "Courier New";
    font-size: 8px;
    padding: 1px 5px;
}}
QPushButton:hover   {{ background: rgba(245,166,35,70); }}
QPushButton:checked {{ background: rgba(245,166,35,110); color: rgb(15,17,23); }}
"""


# ── Main overlay window ───────────────────────────────────────────────────────

class DispatchOverlay(QWidget):

    def __init__(self):
        super().__init__()
        self._collapsed  = False
        self._drag_start = None
        self._rec_frame  = 0

        self._init_window()
        self._build_ui()
        self._start_timers()

    # ── Window flags & positioning ────────────────────────────────────────────

    def _init_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFixedWidth(_WIDTH)
        self.setMouseTracking(True)

        screen = QApplication.primaryScreen().geometry()
        self.move(screen.width() - _WIDTH - 12, 12)

    def showEvent(self, event):
        super().showEvent(event)
        self._click_through(True)

    # ── Click-through (WS_EX_TRANSPARENT) ────────────────────────────────────

    def _click_through(self, enable: bool):
        try:
            import ctypes
            hwnd   = int(self.winId())
            GWL    = -20              # GWL_EXSTYLE
            LAYER  = 0x80000          # WS_EX_LAYERED
            THRU   = 0x20             # WS_EX_TRANSPARENT
            style  = ctypes.windll.user32.GetWindowLongW(hwnd, GWL)
            new    = (style | LAYER | THRU) if enable else (style & ~THRU)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL, new)
        except Exception:
            pass

    def enterEvent(self, event):
        self._click_through(False)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._click_through(True)
        super().leaveEvent(event)

    # ── Drag to reposition ────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drag_start and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_start)

    def mouseReleaseEvent(self, event):
        self._drag_start = None

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.setStyleSheet(_base_qss())

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header (always visible, interactive) ──────────────────────────────
        hdr = QWidget()
        hdr.setFixedHeight(26)
        hdr_l = QHBoxLayout(hdr)
        hdr_l.setContentsMargins(8, 0, 6, 0)
        hdr_l.setSpacing(5)

        self._rec_dot  = QLabel("●")
        self._ptt_lbl  = QLabel("[DEL]")
        self._title    = QLabel("DISPATCH")

        self._rec_dot.setStyleSheet(f"color: {_css_col('dim')}; font-size: 11px;")
        self._ptt_lbl.setStyleSheet(f"color: {_css_col('amber')}; font-size: 8px;")
        self._title.setStyleSheet(
            f"color: {_css_col('amber')}; font-size: 9px; "
            f"font-weight: bold; letter-spacing: 2px;"
        )

        self._mute_btn = QPushButton("MUTE")
        self._mute_btn.setCheckable(True)
        self._mute_btn.setFixedSize(42, 18)
        self._mute_btn.clicked.connect(self._on_mute)

        self._collapse_btn = QPushButton("━")
        self._collapse_btn.setFixedSize(20, 18)
        self._collapse_btn.clicked.connect(self._on_collapse)

        hdr_l.addWidget(self._rec_dot)
        hdr_l.addWidget(self._ptt_lbl)
        hdr_l.addWidget(self._title)
        hdr_l.addStretch(1)
        hdr_l.addWidget(self._mute_btn)
        hdr_l.addWidget(self._collapse_btn)
        root.addWidget(hdr)

        # ── Body (collapsible) ────────────────────────────────────────────────
        self._body = QWidget()
        body_l = QVBoxLayout(self._body)
        body_l.setContentsMargins(7, 3, 7, 7)
        body_l.setSpacing(4)

        # Current job
        self._job = self._panel("CURRENT JOB")
        body_l.addWidget(self._job["w"])

        # Fleet
        self._fleet = self._panel("FLEET")
        body_l.addWidget(self._fleet["w"])

        # Top jobs
        self._market = self._panel("TOP JOBS")
        body_l.addWidget(self._market["w"])

        # Session + fuel
        self._session = self._panel("SESSION / FUEL")
        body_l.addWidget(self._session["w"])

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet("color: rgba(245,166,35,40);")
        body_l.addWidget(div)

        # AI subtitle
        self._subtitle = QLabel("")
        self._subtitle.setWordWrap(True)
        self._subtitle.setStyleSheet(
            "font-size: 10px; color: rgb(240,243,248); "
            "border-left: 2px solid rgb(245,166,35); "
            "padding: 3px 7px;"
        )
        self._subtitle.hide()
        body_l.addWidget(self._subtitle)

        # Transcript echo
        self._transcript_lbl = QLabel("")
        self._transcript_lbl.setWordWrap(True)
        self._transcript_lbl.setStyleSheet(
            f"font-size: 8px; font-style: italic; color: {_css_col('dim')};"
        )
        self._transcript_lbl.hide()
        body_l.addWidget(self._transcript_lbl)

        # History
        self._history_panel = self._panel("HISTORY")
        self._history_panel["lbl"].setStyleSheet(
            f"font-size: 8px; color: {_css_col('dim')};"
        )
        body_l.addWidget(self._history_panel["w"])

        body_l.addStretch(1)
        root.addWidget(self._body)

    def _panel(self, title: str) -> dict:
        """Create a labelled section; returns dict with 'w' (widget) and 'lbl'."""
        frame = QFrame()
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 1, 0, 1)
        fl.setSpacing(1)

        t = QLabel(title)
        t.setStyleSheet(
            f"color: {_css_col('amber')}; "
            f"font-size: 7px; font-weight: bold; letter-spacing: 1px;"
        )
        lbl = QLabel("—")
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        fl.addWidget(t)
        fl.addWidget(lbl)
        return {"w": frame, "lbl": lbl, "title": t}

    # ── Timers ────────────────────────────────────────────────────────────────

    def _start_timers(self):
        self._data_tmr = QTimer(self)
        self._data_tmr.timeout.connect(self._refresh)
        self._data_tmr.start(500)

        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(280)

    # ── Data refresh ──────────────────────────────────────────────────────────

    def _refresh(self):
        try:
            import assistant as _a
            tel, snap, _, _, _ = _a.state.read()
            ov = overlay_state
            self._r_job(snap, tel)
            self._r_fleet(snap)
            self._r_market(snap)
            self._r_session(snap, tel)
            self._r_subtitle(ov)
            self._r_history(ov)
            self._r_ptt_key(_a)
            self._mute_btn.setChecked(ov.muted)
            self._mute_btn.setText("MUTED" if ov.muted else "MUTE")
        except Exception:
            pass
        self.adjustSize()

    def _r_job(self, snap, tel):
        lbl = self._job["lbl"]
        player    = snap.get("player", {}) or {}
        in_job    = player.get("in_job", False)
        live_cargo = (tel.get("cargo") or "").strip()
        live_mass  = float(tel.get("cargo_mass") or 0)

        if in_job or live_mass > 0 or live_cargo:
            cargo   = live_cargo or player.get("job_info_cargo") or "Unknown cargo"
            target  = player.get("job_info_target") or ""
            dist_km = int(player.get("job_info_planned_distance_km") or 0)
            dist_mi = round(dist_km * 0.621371)
            parts   = target.split(".") if target else []
            dest    = parts[-1].replace("_", " ").title() if parts else "Unknown"
            lbl.setText(f"{cargo}\n→ {dest}   {dist_mi} mi")
            lbl.setStyleSheet(f"font-size: 9px; color: {_css_col('bright')};")
        else:
            lbl.setText("No active load")
            lbl.setStyleSheet(f"font-size: 9px; color: {_css_col('dim')};")

    def _r_fleet(self, snap):
        lbl     = self._fleet["lbl"]
        drivers = snap.get("drivers", [])
        if not drivers:
            lbl.setText("—")
            return
        on_job = [d for d in drivers if d.get("state") == 2]
        idle   = [d for d in drivers if d.get("state") != 2]
        lines  = [f"{len(drivers)} drivers  ■ {len(on_job)} on job  □ {len(idle)} idle"]
        for d in idle[:2]:
            city = d.get("current_city", "").replace("_", " ").title()
            did  = d.get("id", "").replace("driver.", "")
            lines.append(f"  [{did}]  {city}")
        lbl.setText("\n".join(lines))
        lbl.setStyleSheet(f"font-size: 9px; color: {_css_col('text')};")

    def _r_market(self, snap):
        lbl = self._market["lbl"]
        fm  = snap.get("freight_market", [])
        if not fm:
            lbl.setText("—")
            return
        top   = sorted(fm, key=lambda j: j.get("revenue", 0), reverse=True)[:3]
        lines = []
        for j in top:
            src = j.get("source_city", "?")
            dst = j.get("destination_city", "?")
            rev = j.get("revenue", 0)
            lines.append(f"{src} → {dst}  ${rev:,}")
        lbl.setText("\n".join(lines))
        lbl.setStyleSheet(f"font-size: 9px; color: {_css_col('text')};")

    def _r_session(self, snap, tel):
        lbl      = self._session["lbl"]
        fuel_pct = float(tel.get("fuel_pct") or 0)
        fin      = snap.get("finances", {}) or {}
        jobs     = snap.get("jobs", [])
        lines    = []

        if fuel_pct > 0:
            n   = 14
            f   = round(fuel_pct / 100 * n)
            bar = "█" * f + "░" * (n - f)
            lines.append(f"FUEL [{bar}]  {fuel_pct:.0f}%")

        if jobs:
            total = sum(j.get("revenue", 0) for j in jobs[:5])
            lines.append(f"RECENT  {len(jobs[:5])} jobs  ${total:,}")

        money = fin.get("money", 0)
        if money:
            lines.append(f"CASH  ${money:,}")

        lbl.setText("\n".join(lines) or "—")
        color = _css_col("red") if 0 < fuel_pct < 20 else _css_col("text")
        lbl.setStyleSheet(f"font-size: 9px; color: {color};")

    def _r_subtitle(self, ov):
        now = time.monotonic()

        # AI response subtitle (fades over SUBTITLE_SECS)
        if ov.last_response and (now - ov.response_time) < _SUBTITLE_SECS:
            age     = now - ov.response_time
            alpha   = max(60, int(255 * (1 - age / _SUBTITLE_SECS)))
            self._subtitle.setText(f'"{ov.last_response}"')
            self._subtitle.setStyleSheet(
                f"font-size: 10px; color: rgba(240,243,248,{alpha}); "
                f"border-left: 2px solid rgba(245,166,35,{alpha}); "
                f"padding: 3px 7px;"
            )
            self._subtitle.show()
        else:
            self._subtitle.hide()

        # Your transcript echo (fades faster)
        if ov.last_transcript and (now - ov.transcript_time) < _TRANSCRIPT_SECS:
            self._transcript_lbl.setText(f"You: {ov.last_transcript}")
            self._transcript_lbl.show()
        else:
            self._transcript_lbl.hide()

    def _r_history(self, ov):
        lbl   = self._history_panel["lbl"]
        items = ov.history[-6:]   # last 3 turns
        lines = []
        for entry in items:
            role = entry.get("role", "")
            text = entry.get("text", "")
            disp = (text[:70] + "…") if len(text) > 70 else text
            prefix = "YOU" if role == "user" else " AI"
            lines.append(f"{prefix}: {disp}")
        lbl.setText("\n".join(lines) or "—")

    def _r_ptt_key(self, assistant_mod):
        try:
            key = (assistant_mod.PTT_KEY_ENV or "delete").upper()
            self._ptt_lbl.setText(f"[{key}]")
        except Exception:
            pass

    # ── Animation ─────────────────────────────────────────────────────────────

    def _animate(self):
        self._rec_frame = (self._rec_frame + 1) % 4
        ov = overlay_state

        if ov.recording:
            filled = self._rec_frame
            dots   = "●" * filled + "○" * (3 - filled)
            self._rec_dot.setText(dots)
            self._rec_dot.setStyleSheet(
                f"color: {_css_col('red')}; font-size: 11px;"
            )
            self._title.setText("● REC")
            self._title.setStyleSheet(
                f"color: {_css_col('red')}; font-size: 9px; "
                f"font-weight: bold; letter-spacing: 2px;"
            )
        else:
            self._rec_dot.setText("●")
            self._rec_dot.setStyleSheet(
                f"color: {_css_col('dim')}; font-size: 11px;"
            )
            self._title.setText("DISPATCH")
            self._title.setStyleSheet(
                f"color: {_css_col('amber')}; font-size: 9px; "
                f"font-weight: bold; letter-spacing: 2px;"
            )

    # ── Button handlers ───────────────────────────────────────────────────────

    def _on_mute(self):
        overlay_state.muted = self._mute_btn.isChecked()
        try:
            import assistant
            assistant._set_tts_muted(overlay_state.muted)
        except Exception:
            pass

    def _on_collapse(self):
        self._collapsed = not self._collapsed
        self._body.setVisible(not self._collapsed)
        self._collapse_btn.setText("□" if self._collapsed else "━")
        self.adjustSize()

    # ── Background paint ──────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(_C["bg"]))
        p.setPen(QPen(_C["border"], 1))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 7, 7)

        # Header bottom border
        p.setPen(QPen(_C["amber_lo"], 1))
        p.drawLine(0, 25, self.width(), 25)
        p.end()


# ── Entry point ───────────────────────────────────────────────────────────────

def start_overlay():
    """
    Create and show the overlay widget in the calling thread.

    IMPORTANT: must be called from the main thread AFTER QApplication has been
    created (client.py does this at the very top of main()).  Qt forbids creating
    widgets outside the main thread.  There is no internal threading here — the
    caller owns the event loop (app.exec() in client.py's main()).

    Returns the DispatchOverlay instance; keep the reference alive so GC does
    not destroy the window.
    """
    if not PYQT6_OK:
        print(
            "[Overlay] PyQt6 not installed — overlay disabled.\n"
            "          Install it with:  pip install PyQt6",
            flush=True,
        )
        return None

    window = DispatchOverlay()
    window.show()
    print("[Overlay] DispatchOverlay created in main thread", flush=True)
    return window
