"""Launcher: keeps the project importable wherever it is unpacked."""

import os
import sys
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "AirPodsSoundLevels", "launch.log")


def note(line):
    """Record one launch event.

    Started by Windows at logon the app has nowhere to report a failure:
    pythonw has no console, and a Python traceback never reaches the event
    log - so a crash at logon looks exactly like never having been started.
    """
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("{} {}\n".format(
                datetime.now().isoformat(timespec="seconds"), line))
    except OSError:
        pass


if __name__ == "__main__":
    note("start")
    try:
        from airpods_levels.app import main
        code = main()
    except BaseException:
        note("crash\n" + traceback.format_exc())
        raise
    note("exit {}".format(code))
    sys.exit(code)
