"""
Exposure window, built like a Mac app.

Structure from the real Weather app on macOS Tahoe; the details that make it
read as Apple rather than as an imitation:

  - Inter, not the Windows system font (SF Pro's licence is Apple-only, Inter
    is the open face drawn against the same brief)
  - Phosphor icons, monoline on a 256 grid, the closest open set to SF Symbols
  - a 26 pt window radius, which is what Tahoe gives a toolbar window
  - real elevation: every surface sits at a height that matches its rank, and
    casts its own shadow. Flat fills on one plane was the main reason nothing
    looked layered.
  - concentric radii: window 26, content inset 16, cards 10
"""

import time
from datetime import datetime, timedelta

import numpy as np
from PySide6.QtCore import (Qt, QRectF, QRect, QTimer, QVariantAnimation,
                            QEasingCurve, QPointF)
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import glass as lg
from . import panel as pnl
from . import i18n
from .i18n import t

SIDEBAR_W = 212
DETAIL_W = 604
W = SIDEBAR_W + DETAIL_W
H = 556
RADIUS = 26              # macOS Tahoe toolbar-window radius
INSET = 16
CARD_RADIUS = RADIUS - INSET     # concentric
PAD = pnl.SHADOW_PAD
GLASS_MS = 33                    # floor; the real pace follows the work

WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

TL_CLOSE = QColor("#FF5F57")
TL_MIN = QColor("#FEBC2E")
TL_ZOOM = QColor("#28C840")
TL_GLYPH = QColor(0, 0, 0, 150)

LABEL = QColor(255, 255, 255)
LABEL2 = QColor(235, 235, 245, 158)
LABEL3 = QColor(235, 235, 245, 115)
LABEL4 = QColor(235, 235, 245, 76)

# The floor. Kept translucent on purpose: at 222 it sealed the window shut
# and the refracted desktop never showed anywhere except the sidebar strip.
DETAIL_FILL = QColor(18, 18, 21, 148)
CARD = QColor(255, 255, 255, 34)        # primary content, lifted highest
TILE = QColor(255, 255, 255, 19)        # secondary, one step down
SELECTED = QColor(10, 132, 255, 210)
HOVER = QColor(255, 255, 255, 24)
HAIRLINE = QColor(255, 255, 255, 18)

# Elevation ladder. Height is the ranking, and in dark mode height reads as
# a lighter fill plus a brighter top edge; the shadow only sharpens the join.
ELEV_CARD = dict(blur=15.0, alpha=0.52, dy=5, highlight=34)
ELEV_TILE = dict(blur=8.0, alpha=0.38, dy=3, highlight=20)
ELEV_ROW = dict(blur=5.0, alpha=0.32, dy=2, highlight=26)


TYPE = {
    "hero":     (54, QFont.ExtraLight, -3.2),
    "value":    (16, QFont.DemiBold, -0.3),
    "headline": (11, QFont.DemiBold, -0.05),
    "sidebar":  (10, QFont.Medium, -0.05),
    "body":     (10, QFont.Normal, 0.0),
    "label":    (8, QFont.Bold, 0.7),     # small caps, Weather card style
    "caption":  (8, QFont.Normal, 0.1),
}



def _font(style):
    size, weight, tracking = TYPE[style]
    return pnl.font(size, weight, tracking)


def _text(p, rect, flags, text, color):
    p.setPen(color)
    p.drawText(rect, flags, text)


def dose_color(dose):
    if dose >= 1.0:
        return pnl.RED
    if dose >= 0.5:
        return pnl.YELLOW
    return pnl.GREEN


def level_color(spl):
    _, accent, _ = pnl.level_style(spl)
    return accent or pnl.GREEN


class HistoryWindow(QWidget):
    def __init__(self, store):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Window
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.store = store
        self.day = datetime.now().date()
        self._drag = None
        self._glass = None
        self._pressed = None
        self._hover = None
        self._lights_hot = False
        self._t = 1.0
        self.sidebar_open = True

        self.renderer = None
        self._renderers = {}
        self._build_renderer()

        self.anim = QVariantAnimation(self)
        self.anim.valueChanged.connect(self._on_anim)

        self.reload()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.reload)
        self.timer.start(5000)

        # Self-pacing: a fixed interval shorter than the work saturates the
        # event loop and frames get dropped, which reads far worse than a
        # lower steady rate. Each tick schedules the next from what the last
        # one actually cost.
        self.glass_timer = QTimer(self)
        self.glass_timer.setSingleShot(True)
        self.glass_timer.timeout.connect(self._tick_glass)
        self._last_ms = GLASS_MS

    def _tick_glass(self):
        t0 = time.perf_counter()
        self.refresh_glass(False)
        self._last_ms = (time.perf_counter() - t0) * 1000.0
        if self.isVisible():
            self.glass_timer.start(int(max(GLASS_MS, self._last_ms * 1.25)))

    # -- geometry -----------------------------------------------------------

    def _sw(self):
        return SIDEBAR_W if self.sidebar_open else 0

    def _w(self):
        return self._sw() + DETAIL_W

    def _build_renderer(self):
        """Full resolution. Half res was 4x cheaper but destroyed the bezel -
        the bezel is a sharp feature, not part of the blurred layer, so it
        cannot survive being computed small and scaled back up.

        The geometry is supersampled, which costs close to a second to build,
        so each width is built once and kept: collapsing the sidebar and
        opening it again is then instant.
        """
        w = self._w()
        if w not in self._renderers:
            self._renderers[w] = pnl.PanelRenderer(w, H, RADIUS,
                                                   glass=pnl.GLASS_WINDOW,
                                                   pad=PAD)
        self.renderer = self._renderers[w]
        self._last_sig = None
        self.resize(self.renderer.cw, self.renderer.ch)

    def toggle_sidebar(self):
        self.sidebar_open = not self.sidebar_open
        self._build_renderer()
        self.refresh_glass()

    # -- glass --------------------------------------------------------------

    def _set_affinity(self, value):
        """Hide this window from screen capture for the length of one grab,
        so the glass never refracts itself into a feedback loop."""
        try:
            import ctypes
            return bool(ctypes.windll.user32.SetWindowDisplayAffinity(
                ctypes.c_void_p(int(self.winId())), ctypes.c_uint(value)))
        except Exception:
            return False

    def refresh_glass(self, force=True):
        w = self._w()
        gw, gh = self.renderer.w, self.renderer.h
        excluded = self._set_affinity(WDA_EXCLUDEFROMCAPTURE) \
            if self.isVisible() else False
        if self.isVisible() and not excluded:
            return
        try:
            pos = self.pos()
            backdrop = lg.grab_screen(pos.x() + PAD, pos.y() + PAD, gw, gh)
        except Exception:
            backdrop = None
        finally:
            if excluded:
                self._set_affinity(WDA_NONE)
        if backdrop is None or backdrop.shape[:2] != (gh, gw):
            backdrop = np.full((gh, gw, 3), 18, dtype=np.uint8)

        # the desktop behind is usually still; skip the 24 ms refraction when
        # nothing moved and pay only the capture
        sig = float(backdrop[::7, ::7].sum(dtype=np.float64))
        # relative, not absolute: sig sums thousands of samples, so an
        # absolute epsilon never matched and the skip never fired
        if not force and self._last_sig is not None \
                and abs(sig - self._last_sig) <= 3e-4 * max(sig, 1.0):
            return
        self._last_sig = sig

        self._glass = self.renderer.glass_image_split(backdrop)
        self.update()

    def hideEvent(self, event):
        self.glass_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.refresh_glass)
        self.glass_timer.start(GLASS_MS)
        self._t = 0.0
        self.anim.stop()
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setDuration(300)
        self.anim.setEasingCurve(QEasingCurve.OutQuint)
        self.anim.start()

    def _on_anim(self, value):
        self._t = float(value)
        self.setWindowOpacity(min(1.0, self._t * 1.8))
        self.update()

    def reload(self):
        self.week = self.store.recent_days(9)
        self.stats = self.store.day_stats(self.day)
        self.update()

    # -- hit targets --------------------------------------------------------

    def _lights(self):
        y = PAD + 16
        return {"close": QRect(PAD + 18, y, 12, 12),
                "min": QRect(PAD + 38, y, 12, 12),
                "zoom": QRect(PAD + 58, y, 12, 12)}

    def _toggle_rect(self):
        return QRect(PAD + 86, PAD + 11, 22, 22)

    def _rows(self):
        out = {}
        if not self.sidebar_open:
            return out
        top = PAD + 62
        for i, (date, _) in enumerate(reversed(self.week)):
            out[date] = QRect(PAD + 9, top + i * 32, SIDEBAR_W - 18, 28)
        return out

    def _hit(self, pos):
        for name, r in self._lights().items():
            if r.adjusted(-3, -3, 3, 3).contains(pos):
                return ("light", name)
        if self._toggle_rect().contains(pos):
            return ("toggle", None)
        for date, r in self._rows().items():
            if r.contains(pos):
                return ("day", date)
        return None

    # -- interaction --------------------------------------------------------

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        elif event.key() == Qt.Key_Left:
            self._shift(-1)
        elif event.key() == Qt.Key_Right:
            self._shift(1)

    def _shift(self, delta):
        new = self.day + timedelta(days=delta)
        if new <= datetime.now().date():
            self.day = new
            self.reload()

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        hit = self._hit(pos)
        if hit:
            self._pressed = hit
            self.update()
        elif pos.x() > PAD + self._sw() or pos.y() < PAD + 46:
            self._drag = event.globalPosition().toPoint() - self.pos()

    def mouseReleaseEvent(self, event):
        pos = event.position().toPoint()
        if self._pressed:
            if self._hit(pos) == self._pressed:
                kind, what = self._pressed
                if kind == "light" and what in ("close", "min"):
                    self.close()
                elif kind == "light" and what == "zoom":
                    self.toggle_sidebar()
                elif kind == "toggle":
                    self.toggle_sidebar()
                elif kind == "day":
                    self.day = what
                    self.reload()
            self._pressed = None
            self.update()
        if self._drag is not None:
            self._drag = None
            self.refresh_glass()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag)
            return
        pos = event.position().toPoint()
        # macOS reveals all three glyphs when the pointer nears the cluster
        cluster = QRect(PAD + 12, PAD + 10, 60, 24)
        hot = cluster.contains(pos)
        hit = self._hit(pos)
        hover = hit[1] if hit and hit[0] == "day" else None
        if hover != self._hover or hot != self._lights_hot:
            self._hover, self._lights_hot = hover, hot
            self.update()

    def leaveEvent(self, event):
        self._hover, self._lights_hot = None, False
        self.update()

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)

        s = 0.985 + 0.015 * self._t
        p.translate(self.width() / 2.0, self.height() / 2.0)
        p.scale(s, s)
        p.translate(-self.width() / 2.0, -self.height() / 2.0)

        if self._glass is not None:
            p.drawImage(0, 0, self._glass)

        p.translate(PAD, PAD)

        sw = self._sw()
        shell = pnl.squircle_path(QRectF(0, 0, self._w(), H), RADIUS)
        p.save()
        p.setClipPath(shell)
        # the detail pane is the floor; the sidebar floats above it, so the
        # shadow falls from the sidebar onto the detail, not the reverse
        p.fillRect(QRectF(sw, 0, DETAIL_W, H), DETAIL_FILL)
        if sw:
            # short falloff: long enough to read as a cast shadow, short
            # enough not to look like a smudge
            for i in range(5):
                a = int(42 * (1 - i / 5.0) ** 2)
                p.fillRect(QRectF(sw + i, 0, 1, H), QColor(0, 0, 0, a))
            p.fillRect(QRectF(sw - 1, 0, 1, H), HAIRLINE)
            self._draw_sidebar(p)
        self._draw_detail(p, sw, 0, DETAIL_W, H)
        p.restore()

        self._draw_lights(p)
        self._draw_toggle(p)
        p.end()

    def _draw_toggle(self, p):
        r = QRectF(self._toggle_rect().translated(-PAD, -PAD))
        if self._pressed == ("toggle", None):
            p.fillPath(pnl.squircle_path(r, 5), QColor(255, 255, 255, 30))
        colour = LABEL2 if self.sidebar_open else LABEL4
        glyph = pnl._icon("sidebar", colour, 15)
        p.drawImage(int(r.center().x() - 7), int(r.center().y() - 7), glyph)

    def _draw_lights(self, p):
        glyphs = {"close": TL_CLOSE, "min": TL_MIN, "zoom": TL_ZOOM}
        p.setPen(Qt.NoPen)
        for name, r in self._lights().items():
            rf = QRectF(r.translated(-PAD, -PAD))
            c = glyphs[name]
            if self._pressed == ("light", name):
                c = c.darker(130)
            p.setBrush(c)
            p.drawEllipse(rf)

        if self._lights_hot:
            p.setBrush(Qt.NoBrush)
            pen = QPen(TL_GLYPH, 1.3, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            for name, r in self._lights().items():
                rf = QRectF(r.translated(-PAD, -PAD))
                c, d = rf.center(), 2.6
                if name == "close":
                    p.drawLine(QPointF(c.x() - d, c.y() - d),
                               QPointF(c.x() + d, c.y() + d))
                    p.drawLine(QPointF(c.x() + d, c.y() - d),
                               QPointF(c.x() - d, c.y() + d))
                elif name == "min":
                    p.drawLine(QPointF(c.x() - d - 0.6, c.y()),
                               QPointF(c.x() + d + 0.6, c.y()))
                else:
                    p.drawLine(QPointF(c.x() - d, c.y() + d),
                               QPointF(c.x() + d, c.y() - d))
        p.setBrush(Qt.NoBrush)

    # -- sidebar ------------------------------------------------------------

    def _draw_sidebar(self, p):
        p.setFont(_font("label"))
        _text(p, QRectF(19, 44, SIDEBAR_W - 38, 12),
              Qt.AlignLeft | Qt.AlignVCenter, t("window.recent_days"), LABEL3)

        today = datetime.now().date()
        table = dict(self.week)
        for date, r in self._rows().items():
            rf = QRectF(r.translated(-PAD, -PAD))
            stats = table[date]
            selected = date == self.day
            path = pnl.squircle_path(rf, 6)

            if selected:
                pnl.draw_elevated(p, path, rf, SELECTED, 6, **ELEV_ROW)
            elif self._hover == date:
                p.fillPath(path, HOVER)

            if date == today:
                name = t("window.today")
            elif date == today - timedelta(days=1):
                name = t("window.yesterday")
            else:
                name = i18n.short_date(date)

            p.setFont(_font("sidebar"))
            _text(p, rf.adjusted(11, 0, -50, 0), Qt.AlignLeft | Qt.AlignVCenter,
                  name, LABEL if selected else LABEL2)

            dose = stats["dose"]
            p.setFont(_font("caption"))
            _text(p, rf.adjusted(0, 0, -11, 0), Qt.AlignRight | Qt.AlignVCenter,
                  i18n.percent(dose * 100) if dose > 0 else "--",
                  LABEL if selected else
                  (dose_color(dose) if dose >= 0.5 else LABEL4))

        self._draw_bottom_bar(p)

    def _draw_bottom_bar(self, p):
        """A Mac sidebar ends in a bottom bar carrying status, not empty space."""
        bar_h = 34
        top = H - bar_h
        p.fillRect(QRectF(0, top, SIDEBAR_W, 1), HAIRLINE)

        dot = QRectF(18, top + bar_h / 2 - 3, 6, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(pnl.GREEN)
        p.drawEllipse(dot)
        p.setBrush(Qt.NoBrush)

        p.setFont(_font("caption"))
        _text(p, QRectF(32, top, SIDEBAR_W - 44, bar_h),
              Qt.AlignLeft | Qt.AlignVCenter, t("window.recording"), LABEL3)

    # -- detail -------------------------------------------------------------

    def _card_label(self, p, x, y, icon, text):
        p.drawImage(int(x), int(y), pnl._icon(icon, LABEL3, 10))
        p.setFont(_font("label"))
        _text(p, QRectF(x + 15, y - 1, 340, 12), Qt.AlignLeft | Qt.AlignVCenter,
              text.upper(), LABEL3)

    def _draw_detail(self, p, x, y, w, h):
        s = self.stats
        dose = s["dose"]
        today = datetime.now().date()

        if self.day == today:
            when = t("window.today")
        elif self.day == today - timedelta(days=1):
            when = t("window.yesterday")
        else:
            when = i18n.short_date(self.day)

        full = i18n.full_date(self.day)
        p.setFont(_font("label"))
        _text(p, QRectF(x, y + 28, w, 13), Qt.AlignCenter, full.upper(), LABEL3)

        p.setFont(_font("hero"))
        _text(p, QRectF(x, y + 42, w, 66), Qt.AlignCenter,
              i18n.percent(dose * 100), LABEL)

        if dose >= 1.0:
            note = t("window.dose_over")
        elif dose >= 0.5:
            note = t("window.dose_watch")
        elif dose > 0:
            note = t("window.dose_safe")
        else:
            note = t("window.dose_none")
        p.setFont(_font("headline"))
        _text(p, QRectF(x, y + 106, w, 17), Qt.AlignCenter, note,
              dose_color(dose))
        p.setFont(_font("caption"))
        _text(p, QRectF(x, y + 123, w, 13), Qt.AlignCenter,
              t("window.daily_dose"), LABEL4)

        cx, cw = x + INSET, w - INSET * 2
        self._draw_chart_card(p, cx, y + 146, cw, 172)
        self._draw_tiles(p, cx, y + 330, cw, 186)

    def _draw_chart_card(self, p, x, y, w, h):
        rect = QRectF(x, y, w, h)
        pnl.draw_elevated(p, pnl.squircle_path(rect, CARD_RADIUS), rect, CARD,
                          CARD_RADIUS, **ELEV_CARD)
        self._card_label(p, x + 14, y + 13, "waveform", t("window.chart_title"))

        lo, hi = pnl.DB_LO, pnl.DB_HI
        gutter = 24
        gx, gy = x + 14 + gutter, y + 38
        gw, gh = w - 28 - gutter, h - 64

        p.setFont(_font("caption"))
        for level, colour in ((80.0, pnl.YELLOW), (95.0, pnl.RED)):
            ly = gy + gh - (level - lo) / (hi - lo) * gh
            pen = QPen(HAIRLINE, 1, Qt.DashLine)
            pen.setDashPattern([1, 3])
            p.setPen(pen)
            p.drawLine(int(gx), int(ly), int(gx + gw), int(ly))
            _text(p, QRectF(x + 10, ly - 7, gutter - 4, 14),
                  Qt.AlignRight | Qt.AlignVCenter, "{:.0f}".format(level),
                  QColor(colour.red(), colour.green(), colour.blue(), 185))

        rows = self.stats["rows"]
        if rows:
            day_start = datetime(self.day.year, self.day.month,
                                 self.day.day).timestamp()
            step = 3.0
            cols = {}
            for minute, leq, peak, seconds in rows:
                c = int((minute - day_start) / 86400.0 * gw / step)
                cols[c] = max(cols.get(c, -999), leq)
            for c, leq in cols.items():
                v = max(0.0, min(1.0, (leq - lo) / (hi - lo)))
                bh = max(step, v * gh)
                bx = gx + c * step
                if bx + step > gx + gw:
                    continue
                p.fillPath(pnl.squircle_path(
                    QRectF(bx, gy + gh - bh, step - 0.8, bh), step / 2),
                    level_color(leq))
        else:
            p.setFont(_font("body"))
            _text(p, QRectF(gx, gy, gw, gh), Qt.AlignCenter,
                  t("window.empty"), LABEL4)

        p.setFont(_font("caption"))
        for hour in (0, 6, 12, 18, 24):
            hx = gx + hour / 24.0 * gw
            _text(p, QRectF(hx - 20, gy + gh + 6, 40, 13), Qt.AlignCenter,
                  "{:02d}".format(hour % 24), LABEL4)

    def _draw_tiles(self, p, x, y, w, h):
        s = self.stats
        gap = 10
        tw = (w - gap * 2) / 3.0
        th = (h - gap) / 2.0

        tiles = [
            ("chart-bar", t("tile.average"),
             "{:.0f} dB".format(s["leq"]) if s["leq"] else "--",
             t("tile.average_note")),
            ("trend-up", t("tile.peak"),
             "{:.0f} dB".format(s["peak"]) if s["peak"] else "--",
             t("tile.peak_note")),
            ("clock", t("tile.listening"), i18n.duration(s["seconds"]),
             t("tile.listening_note")),
            ("warning", t("tile.over80"), i18n.duration(s["over80"]),
             t("tile.over80_note")),
            ("octagon", t("tile.over95"), i18n.duration(s["over95"]),
             t("tile.over95_note")),
        ]

        for i, (icon, label, value, note) in enumerate(tiles):
            col, row = i % 3, i // 3
            tx = x + col * (tw + gap)
            ty = y + row * (th + gap)
            span = tw * 2 + gap if i == len(tiles) - 1 else tw
            rect = QRectF(tx, ty, span, th)
            pnl.draw_elevated(p, pnl.squircle_path(rect, CARD_RADIUS), rect,
                              TILE, CARD_RADIUS, **ELEV_TILE)
            self._card_label(p, tx + 13, ty + 12, icon, label)
            p.setFont(_font("value"))
            _text(p, QRectF(tx + 13, ty + 32, span - 26, 26),
                  Qt.AlignLeft | Qt.AlignVCenter, value, LABEL)
            p.setFont(_font("caption"))
            _text(p, QRectF(tx + 13, ty + 58, span - 26, 13),
                  Qt.AlignLeft | Qt.AlignVCenter, note, LABEL4)
