"""Print the latest rows of the SQLite audit log.

    python show_events.py            # last 30 events
    python show_events.py 100 WARN   # last 100 events with severity WARN or ERROR
"""
import sqlite3
import sys

import config

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30
min_sev = sys.argv[2].upper() if len(sys.argv) > 2 else "INFO"
severities = {"INFO": ("INFO", "WARN", "ERROR"), "WARN": ("WARN", "ERROR"), "ERROR": ("ERROR",)}[min_sev]

db = sqlite3.connect(config.SQLITE_PATH)
rows = db.execute(
    f"SELECT ts_utc, severity, COALESCE(device,'-'), event_type, COALESCE(detail,'') FROM events "
    f"WHERE severity IN ({','.join('?' * len(severities))}) ORDER BY id DESC LIMIT ?",
    (*severities, limit),
).fetchall()
for ts, sev, dev, ev, detail in reversed(rows):
    print(f"{ts[:23]}  {sev:<5}  {dev:<8}  {ev:<22}  {detail}")
print(f"\n{db.execute('SELECT COUNT(*) FROM events').fetchone()[0]} events total in {config.SQLITE_PATH}")
