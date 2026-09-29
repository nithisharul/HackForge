#!/usr/bin/env python3
"""Seed the learning runbook with ONE demo resolution, so the very first
"fatal burst" (or "new errors") incident in a demo already shows a Proven fix.

    python scripts/seed_demo_fix.py --operator "Nithish"
    python scripts/seed_demo_fix.py --list      # show what's stored
    python scripts/seed_demo_fix.py --remove    # delete the demo record(s)

The record is stored with incident id DEMO-SEED-1 so it's easy to identify and
remove. Tell your audience it's seeded demo data.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.db.store import AlertStore  # noqa: E402

# The unusual error types the simulator produces in "fatal burst" / "new errors"
FINGERPRINT = {
    "templates": sorted([
        "Circuit breaker OPEN for payment-gateway after <NUM> failures",
        "Connection refused to db-primary <IP>",
        "Disk quota exceeded on <PATH> volume <NUM>",
        "OutOfMemoryError: Java heap space in worker-<NUM>",
    ]),
    "kind": "error_spike",
}

ACTIONS = [
    "Checked db-primary health: not accepting connections",
    "Failed over to db-replica",
    "Restarted payment workers to clear the OutOfMemory errors",
    "Cleared old logs on /var/lib/data to free disk space",
]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--operator", default="Demo operator", help="name shown as 'last by'")
    p.add_argument("--minutes", type=float, default=6.0, help="time it took to resolve")
    p.add_argument("--times", type=int, default=2, help="how many past incidents it 'worked' for")
    p.add_argument("--db", type=Path, default=settings.db_path)
    p.add_argument("--list", action="store_true")
    p.add_argument("--remove", action="store_true")
    a = p.parse_args()

    AlertStore(a.db).close()                      # make sure all tables exist
    con = sqlite3.connect(a.db)
    if a.remove:
        n = con.execute("DELETE FROM resolutions WHERE incident_id LIKE 'DEMO-SEED-%'").rowcount
        con.commit()
        print(f"removed {n} demo record(s) from {a.db}")
        return
    if a.list:
        for row in con.execute("SELECT incident_id, operator, steps, ttr_seconds, success, actions FROM resolutions"):
            print(row)
        return

    now = datetime.now(timezone.utc)
    for i in range(1, a.times + 1):
        con.execute(
            "INSERT OR REPLACE INTO resolutions "
            "(incident_id, created_at, operator, fingerprint, actions, steps, ttr_seconds, success) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (f"DEMO-SEED-{i}", (now - timedelta(days=i)).isoformat(), a.operator,
             json.dumps(FINGERPRINT), json.dumps(ACTIONS), len(ACTIONS), a.minutes * 60))
    con.commit()
    print(f"seeded {a.times} demo resolution(s) into {a.db}")
    print("Now inject 'fatal burst' (or 'new errors'): the alert will show a Proven fix with these steps:")
    for n, act in enumerate(ACTIONS, 1):
        print(f"  {n}. {act}")


if __name__ == "__main__":
    main()