#!/usr/bin/env python3
"""Continuously append realistic log lines to a file (the "system under watch").

    python scripts/generate_logs.py                       # 20 lines/s into data/app.log
    python scripts/generate_logs.py --inject error_spike@180:120
          # start an error spike 180 s from now, lasting 120 s
    python scripts/generate_logs.py --rotate-every 600    # test rotation handling

The dashboard's "inject" buttons also work with this script: the API drops a
small JSON request file which this script picks up within a second.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.sim.simulator import SCENARIOS, LogSimulator  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", type=Path, default=ROOT / "data" / "app.log")
    p.add_argument("--rate", type=float, default=20.0, help="lines per second")
    p.add_argument("--inject", action="append", default=[],
                   help="scenario@start_s:duration_s, e.g. error_spike@180:120. "
                        f"Scenarios: {', '.join(SCENARIOS)}")
    p.add_argument("--control-file", type=Path, default=ROOT / "data" / "inject_request.json")
    p.add_argument("--rotate-every", type=float, default=0, help="rotate the file every N seconds")
    p.add_argument("--seed", type=int, default=None)
    a = p.parse_args()

    a.file.parent.mkdir(parents=True, exist_ok=True)
    sim = LogSimulator(rate=a.rate, seed=a.seed)
    now = time.time()
    for spec in a.inject:
        kind, _, rest = spec.partition("@")
        start, _, dur = rest.partition(":")
        sim.inject(kind, now + float(start or 0), float(dur or 120))
        print(f"scheduled {kind} at +{start}s for {dur}s")

    print(f"writing ~{a.rate}/s to {a.file} (Ctrl+C to stop)")
    last = time.time()
    last_rotate = last
    total = 0
    try:
        while True:
            time.sleep(0.2)
            now = time.time()
            lines = sim.lines_for_interval(last, now)
            last = now
            if lines:
                with open(a.file, "a") as fh:
                    fh.write("\n".join(lines) + "\n")
                total += len(lines)

            if a.control_file.exists():
                try:
                    req = json.loads(a.control_file.read_text())
                    sim.inject(req["kind"], now, float(req.get("duration_seconds", 120)))
                    print(f"[{time.strftime('%H:%M:%S')}] injected {req['kind']} for {req.get('duration_seconds')}s")
                except Exception as exc:
                    print(f"bad inject request: {exc}")
                a.control_file.unlink(missing_ok=True)

            if a.rotate_every and now - last_rotate >= a.rotate_every:
                os.replace(a.file, a.file.with_suffix(a.file.suffix + ".1"))
                last_rotate = now
                print(f"[{time.strftime('%H:%M:%S')}] rotated log")

            active = [x.kind for x in sim.anomalies if x.active(now)]
            print(f"\r{total:,} lines written  active anomalies: {active or '-'}      ", end="", flush=True)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
