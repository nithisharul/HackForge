"""Blends the calibrated detector scores into one final score.

    final = max( zscore_norm , weighted_mean(all available normalized scores) )

* The statistical z-score is the always-on safety net, so ML can RAISE the
  score (catching things the error rate alone misses) but never hide a clear
  error spike by averaging it away.
* ML scores above 1.0 are log-compressed (1 + ln x) before averaging. Beyond
  the edge of normal, percentile calibration can't tell "20x" from "5000x"
  apart meaningfully, and one model far out on a limb (e.g. the LSTM-AE still
  remembering a spike that ended) should not single-handedly drive severity.
* Missing models (not trained, torch not installed, still filling a sequence)
  are simply left out of the mean.
"""
from __future__ import annotations

import math

from app.models.schemas import DetectorScore


def compress(x: float) -> float:
    return x if x <= 1.0 else 1.0 + math.log(x)


def blend(scores: list[DetectorScore], weights: dict[str, float]) -> float:
    avail = [s for s in scores if s.available]
    if not avail:
        return 0.0
    num = sum(weights.get(s.name, 1.0) * (s.normalized if s.name == "zscore" else compress(s.normalized))
              for s in avail)
    den = sum(weights.get(s.name, 1.0) for s in avail)
    mean = num / den if den else 0.0
    z = next((s.normalized for s in avail if s.name == "zscore"), 0.0)
    return max(z, mean)
