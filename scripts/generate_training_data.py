#!/usr/bin/env python3
"""Generate offline datasets (all SIMULATED):

  data/train_normal.log   - N hours of normal traffic, used to train the models
  data/test_labeled.log   - normal traffic with every anomaly scenario injected
  data/test_labels.json   - ground-truth anomaly intervals for evaluation

    python scripts/generate_training_data.py --hours 6 --test-hours 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.sim.simulator import SCENARIOS, LogSimulator  # noqa: E402


def write(sim: LogSimulator, start: float, seconds: float, path: Path) -> int:
    n = 0
    with open(path, "w") as fh:
        for _t, line in sim.generate(start, seconds):
            fh.write(line + "\n")
            n += 1
    return n


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=float, default=6.0)
    p.add_argument("--test-hours", type=float, default=2.0)
    p.add_argument("--rate", type=float, default=20.0, help="lines/sec (match the live generator)")
    p.add_argument("--out", type=Path, default=ROOT / "data")
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    start = 1_780_000_000.0  # fixed epoch so datasets are reproducible
    n = write(LogSimulator(rate=a.rate, seed=a.seed), start, a.hours * 3600, a.out / "train_normal.log")
    print(f"train_normal.log: {n:,} lines ({a.hours}h normal)")

    # test set: 10 min normal warm-up, then anomalies every 12 minutes
    test_start = start + a.hours * 3600 + 3600
    sim = LogSimulator(rate=a.rate, seed=a.seed + 1)
    anomalies = []
    t = test_start + 600
    kinds = list(SCENARIOS)
    i = 0
    while t + 300 < test_start + a.test_hours * 3600:
        kind = kinds[i % len(kinds)]
        duration = 300 if kind == "slow_ramp" else 180
        sim.inject(kind, t, duration)
        anomalies.append({"kind": kind, "start": t, "end": t + duration})
        t += duration + 720
        i += 1
    n = write(sim, test_start, a.test_hours * 3600, a.out / "test_labeled.log")
    (a.out / "test_labels.json").write_text(json.dumps({
        "description": "SIMULATED data from app/sim/simulator.py",
        "anomalies": anomalies,
    }, indent=2))
    print(f"test_labeled.log: {n:,} lines, {len(anomalies)} injected anomalies")


if __name__ == "__main__":
    main()
