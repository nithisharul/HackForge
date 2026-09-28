"""Puts every detector's raw score on one shared scale.

For each model we look at its raw scores on held-out NORMAL data and record the
median and the 99th percentile. Then:

    normalized = (raw - median) / (p99 - median)

so 0 is typical, 1.0 is the edge of normal (only 1% of normal windows score
higher), 2.0 is twice as far out, and so on. The z-score detector uses z / 3 so
that z = 3 also lands on 1.0. Because the scale is anchored to a percentile, we
can state the expected false-alarm rate of each severity threshold.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

Z_EDGE = 3.0


@dataclass
class Calibration:
    median: float
    p99: float
    p999: float
    n: int

    @classmethod
    def fit(cls, normal_scores: np.ndarray) -> "Calibration":
        s = np.asarray(normal_scores, dtype=float)
        med, p99, p999 = np.percentile(s, [50, 99, 99.9])
        if p99 - med < 1e-9:
            p99 = med + 1e-6
        return cls(float(med), float(p99), float(p999), int(len(s)))

    def normalize(self, raw: float | np.ndarray):
        out = (np.asarray(raw, dtype=float) - self.median) / (self.p99 - self.median)
        out = np.clip(out, 0.0, None)
        return float(out) if np.ndim(out) == 0 else out


def normalize_z(z: float | None) -> float:
    if z is None:
        return 0.0
    return max(z, 0.0) / Z_EDGE


def save_calibrations(cals: dict[str, Calibration], path: Path) -> None:
    path.write_text(json.dumps({k: asdict(v) for k, v in cals.items()}, indent=2))


def load_calibrations(path: Path) -> dict[str, Calibration]:
    if not path.exists():
        return {}
    return {k: Calibration(**v) for k, v in json.loads(path.read_text()).items()}
