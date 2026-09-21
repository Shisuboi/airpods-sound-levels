"""
Niveau sonore AirPods - tray application.

A tray icon shows the current A-weighted level; clicking it opens a Liquid
Glass panel styled after the iOS Control Center headphone module.

The glass refracts the live desktop behind it. Capturing that region while the
panel is on screen would make it refract itself, so the window is flagged
WDA_EXCLUDEFROMCAPTURE for the few milliseconds of the grab and flipped back
straight after - which keeps the panel visible in the user's own screenshots.
"""

import ctypes
import os
import sys
import subprocess
import winreg

import numpy as np
from PySide6.QtCore import (Qt, QTimer, QPoint, QVariantAnimation,
                            QEasingCurve, Property, QObject)
from PySide6.QtGui import (QAction, QColor, QFont, QIcon, QImage, QPainter,
                           QPixmap)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from . import glass as lg
from . import panel as pnl
from .history import History
from .history_window import HistoryWindow
from .meter import SplEstimator
from . import devices
from .i18n import t

HERE = os.path.dirname(os.path.abspath(__file__))

REFRESH_MS = 16           # 58 fps measured while the panel is open.
# The old hard-coded 33 ms was a guess made before any of this was profiled.
# What the frame actually costs on the 360x176 panel: the GDI grab 4.2 ms,
# the split refraction 4.5 ms, the content pass 1.4 ms, and Qt's compositing
# of a translucent always-on-top window about 4 ms. Rendering the bezel at
# full resolution and the interior at half is what buys the headroom - see
# PanelRenderer.glass_image_split.
TRAY_REFRESH_MS = 700
LOG_MS = 1000             # one exposure sample per second
MARGIN = 12

# The Run registry key looked right on paper - a valid value plus a correct
# Settings > Apps > Startup approval byte - and Explorer still silently never
# invoked it, with no trace anywhere pythonw could report a crash. Pivoting
# to a scheduled task didn't help either: schtasks refused *any* absolute
# /TR path with "Access is denied" on this machine, confirmed straight from
# an interactive terminal, unrelated to this app or its own process. A
# Startup-folder shortcut sidesteps both: it is a plain file write to a
# folder the user already owns, the same mechanism several other apps on
# this machine already rely on successfully.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "AirPodsSoundLevels"


def _startup_folder():
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup")


def _shortcut_path():
    return os.path.join(_startup_folder(), APP_NAME + ".lnk")


def _autostart_target():
    """(exe, launcher): pythonw on run.py - no console window, and the
    launcher puts the project on sys.path itself, so no working directory
    needs to be set on the shortcut."""
    exe = sys.executable
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(candidate):
        exe = candidate
    launcher = os.path.join(os.path.dirname(HERE), "run.py")
    return exe, launcher


def _log(line):
    try:
        log_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                               "AirPodsSoundLevels")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "launch.log"), "a", encoding="utf-8") as fh:
            import datetime
            fh.write(datetime.datetime.now().isoformat(timespec="seconds")
                     + " " + line + chr(10))
    except OSError:
        pass


def _remove_legacy_run_entry():
    """One-time cleanup: delete the old Run-key registration this used to
    use, so a stale entry never sits next to the shortcut."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except OSError:
        pass
    try:
        subprocess.run(["schtasks", "/Delete", "/TN", APP_NAME, "/F"],
                       capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass


def is_autostart():
    _remove_legacy_run_entry()
    return os.path.exists(_shortcut_path())


def set_autostart(on):
    _remove_legacy_run_entry()
    path = _shortcut_path()
    if not on:
        try:
            os.remove(path)
        except OSError:
            pass
        return True
    try:
        import comtypes.client
        exe, launcher = _autostart_target()
        shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
        shortcut = shell.CreateShortcut(path)
        shortcut.TargetPath = exe
        shortcut.Arguments = '"{}"'.format(launcher)
        shortcut.WorkingDirectory = os.path.dirname(launcher)
        shortcut.IconLocation = exe
        shortcut.Save()
        _log("shortcut created at {}".format(path))
        return os.path.exists(path)
    except Exception as exc:
        _log("shortcut creation failed: {!r}".format(exc))
        return False


OPEN_MS, CLOSE_MS = 280, 160

WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# MyDockFinder replaces the taskbar with a macOS-style menu bar and does not
# reserve the space in availableGeometry, so the bar has to be found by hand.
MENU_BAR_CLASSES = ("MyFinderApp",)


def menu_bar_bottom():
    """Bottom edge of a macOS-style menu bar pinned to the top, else 0."""
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                  wintypes.LPARAM)
        screen_w = user32.GetSystemMetrics(0)
        found = []

        def cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value not in MENU_BAR_CLASSES:
                return True
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            wide = (r.right - r.left) >= screen_w * 0.8
            if r.top <= 2 and wide and 0 < r.bottom <= 80:
                found.append(r.bottom)
            return True

        user32.EnumWindows(proc(cb), 0)
        return min(found) if found else 0
    except Exception:
        return 0


# --- tray icon ---------------------------------------------------------------


def make_tray_icon(level_db, playing):
    """The level as a number, tinted by tier. Falls back to the AirPods glyph.

    A Windows tray slot is only ~16-24 px, so an icon *and* a number side by
    side would be unreadable. The number alone carries the information.
    """
    size = 32
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)

    if not playing or level_db is None:
        glyph = pnl._icon("headphones", QColor(190, 190, 200), size - 6)
        p.drawImage(3, 3, glyph)
    else:
        _, accent, _ = pnl.level_style(level_db)
        text = "{:.0f}".format(max(0, min(999, level_db)))
        font = QFont("Segoe UI Variable Display", 1)
        if not font.exactMatch():
            font = QFont("Segoe UI", 1)
        font.setWeight(QFont.Bold)
        font.setPixelSize(size if len(text) < 3 else int(size * 0.72))
        p.setFont(font)
        p.setPen(accent)
        p.drawText(img.rect(), Qt.AlignCenter, text)

    p.end()
    return QIcon(QPixmap.fromImage(img))


# --- panel window ------------------------------------------------------------


class GlassPanel(QWidget):
    def __init__(self, estimator):
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.Tool
                         | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.estimator = estimator
        self.renderer = pnl.PanelRenderer()
        # the window is bigger than the panel: the extra ring holds the shadow
        self.resize(self.renderer.cw, self.renderer.ch)

        self.live_backdrop = True
        self.from_top = False   # set by anchor(): drops from the menu bar
        self._frame = None
        self._closing = False
        self._t = 0.0          # animation progress, 0 hidden .. 1 open

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)

        self.anim = QVariantAnimation(self)
        self.anim.valueChanged.connect(self._on_anim)
        self.anim.finished.connect(self._on_anim_done)

    # -- placement ----------------------------------------------------------

    def anchor(self):
        """Window position such that the *panel* keeps MARGIN from the edges.

        The panel hangs under the menu bar when there is one, because that is
        where its icon lives; otherwise it sits above the taskbar.
        """
        area = QApplication.primaryScreen().availableGeometry()
        pad = self.renderer.pad
        x = int(area.right() - self.renderer.w - MARGIN - pad)

        bar = menu_bar_bottom()
        self.from_top = bar > 0
        if self.from_top:
            y = int(bar + MARGIN - pad)
        else:
            y = int(area.bottom() - self.renderer.h - MARGIN - pad)
        return QPoint(x, y)

    def _hwnd(self):
        try:
            return int(self.winId())
        except Exception:
            return 0

    def _set_affinity(self, value):
        hwnd = self._hwnd()
        if not hwnd:
            return False
        try:
            return bool(ctypes.windll.user32.SetWindowDisplayAffinity(
                ctypes.c_void_p(hwnd), ctypes.c_uint(value)))
        except Exception:
            return False

    def capture_backdrop(self):
        """Grab the desktop under the panel, with the panel itself excluded."""
        pos = self.anchor()
        excluded = False
        if self.isVisible():
            excluded = self._set_affinity(WDA_EXCLUDEFROMCAPTURE)
            if not excluded:
                # cannot hide from the grab; keep the last good backdrop
                return None
        pad = self.renderer.pad
        try:
            # raw bytes, not float: the split renderer shrinks the frame
            # before converting, so the cast rides along with the shrink
            # instead of running over four times as many pixels.
            return lg.grab_screen(pos.x() + pad, pos.y() + pad,
                                  self.renderer.w, self.renderer.h)
        except Exception:
            return None
        finally:
            if excluded:
                self._set_affinity(WDA_NONE)

    # -- show / hide --------------------------------------------------------

    def popup(self):
        self.move(self.anchor())
        self._closing = False

        backdrop = self.capture_backdrop()
        if backdrop is None:
            backdrop = np.full((self.renderer.h, self.renderer.w, 3), 0.12,
                               dtype=np.float32)
        self._glass = self.renderer.glass_image_split(backdrop)
        self.refresh(recapture=False)

        self._t = 0.0
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()

        self.anim.stop()
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setDuration(OPEN_MS)
        self.anim.setEasingCurve(QEasingCurve.OutBack)
        self.anim.start()

        self.timer.start(REFRESH_MS)

    def dismiss(self):
        if self._closing or not self.isVisible():
            return
        self._closing = True
        self.timer.stop()
        self.anim.stop()
        self.anim.setStartValue(self._t)
        self.anim.setEndValue(0.0)
        self.anim.setDuration(CLOSE_MS)
        self.anim.setEasingCurve(QEasingCurve.InQuad)
        self.anim.start()

    def _on_anim(self, value):
        self._t = float(value)
        self.setWindowOpacity(max(0.0, min(1.0, self._t)))
        self.update()

    def _on_anim_done(self):
        if self._closing:
            self._closing = False
            self.hide()

    # -- interaction --------------------------------------------------------

    def focusOutEvent(self, event):
        self.dismiss()
        super().focusOutEvent(event)

    def mousePressEvent(self, event):
        self.dismiss()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Space):
            self.dismiss()

    # -- painting -----------------------------------------------------------

    def refresh(self, recapture=True):
        if self._closing:
            return
        if recapture and self.live_backdrop:
            backdrop = self.capture_backdrop()
            if backdrop is not None:
                self._glass = self.renderer.glass_image_split(backdrop)

        reading = self.estimator.read()
        frame = QImage(self._glass)
        p = QPainter(frame)
        self.renderer.draw_content(
            p, reading["spl"], reading["playing"],
            device=reading["device"] or t("tray.output"),
            volume_pct=reading["volume_pct"])
        p.end()
        self._frame = frame
        self.update()

    def paintEvent(self, event):
        if self._frame is None:
            return
        t = max(0.0, min(1.2, self._t))
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        # grow out of the corner nearest its own icon, the way a Control
        # Center module expands from the control that opened it
        scale = 0.94 + 0.06 * t
        w, h = self.width(), self.height()
        oy = 0 if self.from_top else h
        slide = (1.0 - t) * 10.0
        p.translate(w, oy)
        p.scale(scale, scale)
        p.translate(-w, -oy)
        p.translate(0, -slide if self.from_top else slide)
        p.drawImage(0, 0, self._frame)
        p.end()


# --- application -------------------------------------------------------------


class App:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.estimator = SplEstimator()
        self.estimator.start()

        self.history = History()
        # Built up front, not on first click: its supersampled geometry takes
        # close to a second, and that belongs to launch rather than to the
        # moment the user asks to see the window.
        self.history_window = None
        QTimer.singleShot(1200, self._warm_history)

        self.panel = GlassPanel(self.estimator)

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(make_tray_icon(None, False))
        self.tray.setToolTip(t("app.name"))
        self.tray.activated.connect(self.on_activated)

        menu = QMenu()

        act_history = QAction(t("menu.exposure"), menu)
        act_history.triggered.connect(self.open_history)
        menu.addAction(act_history)
        menu.addSeparator()

        self.act_live = QAction(t("menu.live_backdrop"), menu)
        self.act_live.setCheckable(True)
        self.act_live.setChecked(True)
        self.act_live.toggled.connect(self.toggle_live)
        menu.addAction(self.act_live)


        self.act_boot = QAction(t("menu.startup"), menu)
        self.act_boot.setCheckable(True)
        self.act_boot.setChecked(is_autostart())
        self.act_boot.toggled.connect(self.toggle_autostart)
        menu.addAction(self.act_boot)

        menu.addSeparator()
        act_quit = QAction(t("menu.quit"), menu)
        act_quit.triggered.connect(self.quit)
        menu.addAction(act_quit)

        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.show()

        self.tray_timer = QTimer()
        self.tray_timer.timeout.connect(self.update_tray)
        self.tray_timer.start(TRAY_REFRESH_MS)
        self.update_tray()

        # the exposure log runs whether or not the panel is open
        self.log_timer = QTimer()
        self.log_timer.timeout.connect(self.log_sample)
        self.log_timer.start(LOG_MS)

    def log_sample(self):
        r = self.estimator.read()
        self.history.record(r["spl"] if r["playing"] else None)

    def _warm_history(self):
        if self.history_window is None:
            self.history_window = HistoryWindow(self.history)

    def open_history(self):
        self._warm_history()
        self.history_window.reload()
        screen = QApplication.primaryScreen().availableGeometry()
        win = self.history_window
        win.move(screen.center().x() - win.width() // 2,
                 screen.center().y() - win.height() // 2)
        win.show()
        win.raise_()
        win.activateWindow()

    def toggle_autostart(self, on):
        if not set_autostart(on):
            self.act_boot.setChecked(is_autostart())

    def on_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.panel.isVisible() and not self.panel._closing:
                self.panel.dismiss()
            else:
                self.panel.popup()

    def toggle_live(self, on):
        self.panel.live_backdrop = on

    def update_tray(self):
        r = self.estimator.read()
        self.tray.setIcon(make_tray_icon(r["spl"], r["playing"]))
        if r["playing"] and r["spl"] is not None:
            word, _, _ = pnl.level_style(r["spl"])
            tip = "{:.0f} dB(A) - {}\n{}".format(r["spl"], word,
                                                 r["device"] or "")
        elif r["error"]:
            tip = t("tray.unavailable")
        else:
            tip = "Silence\n{}".format(r["device"] or "")
        self.tray.setToolTip(tip.strip())


    def quit(self):
        self.estimator.stop()
        self.history.close()
        self.tray.hide()
        self.app.quit()

    def run(self):
        return self.app.exec()


def main():
    return App().run()


if __name__ == "__main__":
    sys.exit(main())
