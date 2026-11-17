"""Container worker probe reads only persisted heartbeat; no initialization."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import time


def main():
    path = Path("/app/.local/platform/platform.sqlite3")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as con:
            row = con.execute("SELECT recorded_at FROM operations_heartbeats WHERE component='worker'").fetchone()
        return 0 if row and 0 <= time.time() - row[0] <= 60 else 2
    except (OSError, sqlite3.Error):
        return 2


if __name__ == "__main__":
    sys.exit(main())
