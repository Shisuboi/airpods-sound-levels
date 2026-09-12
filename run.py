"""Launcher: keeps the project importable wherever it is unpacked."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airpods_levels.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
