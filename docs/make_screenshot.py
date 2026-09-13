"""Render the README screenshot: the panel and the exposure window.

Uses seeded demo data and a synthetic wallpaper so the image is reproducible
and carries no personal device name.
"""

import os
import random
import sys
import time
from datetime import datetime, timedelta

import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtCore import Qt, QPoint

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AIRPODS_LEVELS_LANG", "en")

from airpods_levels import history as hist              # noqa: E402
from airpods_levels import history_window as hw         # noqa: E402
from airpods_levels import panel as pnl                 # noqa: E402

DEVICE = "AirPods Pro"
DB = os.path.join(os.environ.get("TEMP", "."), "airpods-levels-demo.db")


def seed(store):
    random.seed(11)
    today = datetime.now().date()
    for back in range(9):
        day = today - timedelta(days=back)
        base = datetime(day.year, day.month, day.day)
        blocks = [(9, 40, 69), (14, 115, 79), (17, 70, 84), (21, 45, 88)]
        if back in (2, 5, 7):
            blocks = [(11, 35, 72)]
        if back == 1:
            blocks.append((22, 35, 96))
        for hour, minutes, level in blocks:
            for m in range(minutes):
                ts = (base + timedelta(hours=hour, minutes=m)).timestamp()
                if ts > time.time():
                    continue
                spl = level + random.uniform(-4.0, 4.0)
                for _ in range(60):
                    store.record(spl + random.uniform(-1.4, 1.4), now=ts)
                store.record(None, now=ts + 60)
    store._flush()


def wallpaper(w, h):
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    v = (np.sin(xs / 150.0 + np.cos(ys / 110.0) * 1.7)
         + np.sin((xs + ys) / 195.0)) * 0.5
    v = (v - v.min()) / (v.max() - v.min())
    return np.dstack([v * 0.13 + 0.03, v * 0.34 + 0.11, v * 0.50 + 0.34])


def main():
    app = QApplication(sys.argv)
    if os.path.exists(DB):
        os.remove(DB)
    store = hist.History(DB)
    seed(store)

    win = hw.HistoryWindow(store)
    win.timer.stop()
    # the window builds its own shell once, over a flat field - it no longer
    # refracts whatever is behind it, so there is nothing to feed it here

    small = pnl.PanelRenderer()
    sback = wallpaper(small.w, small.h).astype(np.float32)
    card = small.render(sback, 81.0, True, device=DEVICE, volume_pct=66)

    margin, gap = 34, 26
    width = margin * 2 + win.width()
    height = margin * 2 + win.height() + gap + card.height()

    sheet = QImage(width, height, QImage.Format_ARGB32)
    tile = wallpaper(width, height)
    raw = (np.clip(tile, 0, 1) * 255).astype(np.uint8).copy()
    p = QPainter(sheet)
    p.drawImage(0, 0, QImage(raw.data, width, height, width * 3,
                             QImage.Format_RGB888))
    p.end()

    win.render(sheet, QPoint(margin, margin))
    p = QPainter(sheet)
    p.drawImage(margin + win.width() - card.width(),
                margin + win.height() + gap, card)
    p.end()

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "screenshot.png")
    sheet.save(out)
    print("wrote", out, sheet.width(), "x", sheet.height())
    store.close()
    os.remove(DB)


if __name__ == "__main__":
    main()
