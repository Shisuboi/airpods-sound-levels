"""
Listening-exposure log.

One row per minute: the energy-average level over that minute, its peak, and
how many seconds actually carried sound. Energy-averaging matters - decibels
are logarithmic, so a plain arithmetic mean of dB values is meaningless.

The data lives in %APPDATA%, not next to the code, so moving or reinstalling
the app never loses the history.
"""

import math
import os
import sqlite3
import time
from datetime import datetime, timedelta

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                       "AirPodsSoundLevels")
DB_PATH = os.path.join(APP_DIR, "history.db")

# NIOSH: 85 dB(A) for 8 h, 3 dB exchange rate, nothing counted below 80.
CRITERION_DB = 85.0
CRITERION_HOURS = 8.0
EXCHANGE_DB = 3.0
DOSE_FLOOR_DB = 80.0


def allowed_hours(spl):
    """How long this level may be listened to in a day."""
    if spl < DOSE_FLOOR_DB:
        return None
    return CRITERION_HOURS * (2.0 ** ((CRITERION_DB - spl) / EXCHANGE_DB))


def dose_fraction(spl, seconds):
    """Share of the daily allowance used by `seconds` at `spl`."""
    hours = allowed_hours(spl)
    if not hours:
        return 0.0
    return (seconds / 3600.0) / hours


class History:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS minutes (
                minute  INTEGER PRIMARY KEY,   -- unix epoch, floored to 60 s
                leq     REAL NOT NULL,         -- energy-average dB(A)
                peak    REAL NOT NULL,
                seconds INTEGER NOT NULL       -- seconds with sound
            )""")
        self._conn.commit()

        self._bucket = None
        self._energy = 0.0
        self._count = 0
        self._peak = None

    # -- writing ------------------------------------------------------------

    def record(self, spl, now=None):
        """Feed one reading, roughly once a second. None means silence."""
        now = now or time.time()
        minute = int(now // 60 * 60)

        if self._bucket is not None and minute != self._bucket:
            self._flush()
        self._bucket = minute

        if spl is not None:
            self._energy += 10.0 ** (spl / 10.0)
            self._count += 1
            self._peak = spl if self._peak is None else max(self._peak, spl)

    def _flush(self):
        if self._count and self._bucket is not None:
            leq = 10.0 * math.log10(self._energy / self._count)
            self._conn.execute(
                "INSERT OR REPLACE INTO minutes VALUES (?, ?, ?, ?)",
                (self._bucket, leq, self._peak or leq, self._count))
            self._conn.commit()
        self._energy, self._count, self._peak = 0.0, 0, None

    def close(self):
        self._flush()
        self._conn.close()

    # -- reading ------------------------------------------------------------

    def _day_bounds(self, day):
        start = datetime(day.year, day.month, day.day)
        return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())

    def day_rows(self, day=None):
        """(minute, leq, peak, seconds) for one calendar day."""
        day = day or datetime.now().date()
        if isinstance(day, datetime):
            day = day.date()
        lo, hi = self._day_bounds(day)
        # include the minute still being accumulated
        self._flush()
        cur = self._conn.execute(
            "SELECT minute, leq, peak, seconds FROM minutes "
            "WHERE minute >= ? AND minute < ? ORDER BY minute", (lo, hi))
        return cur.fetchall()

    def day_stats(self, day=None):
        rows = self.day_rows(day)
        if not rows:
            return {"rows": [], "leq": None, "peak": None, "seconds": 0,
                    "dose": 0.0, "over80": 0, "over95": 0}

        energy = 0.0
        total = 0
        peak = max(r[2] for r in rows)
        dose = 0.0
        over80 = over95 = 0
        for _, leq, _, seconds in rows:
            energy += (10.0 ** (leq / 10.0)) * seconds
            total += seconds
            dose += dose_fraction(leq, seconds)
            if leq >= 80.0:
                over80 += seconds
            if leq >= 95.0:
                over95 += seconds

        return {"rows": rows,
                "leq": 10.0 * math.log10(energy / total) if total else None,
                "peak": peak, "seconds": total, "dose": dose,
                "over80": over80, "over95": over95}

    def recent_days(self, count=7):
        """[(date, stats)] ending today, oldest first."""
        today = datetime.now().date()
        out = []
        for i in range(count - 1, -1, -1):
            d = today - timedelta(days=i)
            out.append((d, self.day_stats(d)))
        return out


