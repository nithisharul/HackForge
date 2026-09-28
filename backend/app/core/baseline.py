"""Adaptive baseline of "normal" error rate.

* EWMA of mean and variance, so the baseline follows slow drift (a deploy that
  permanently changes the noise floor) but not sudden spikes.
* Warm-up: the first N confident snapshots only train the baseline; no z-score
  is reported until then, so the system never alerts on its first minute.
* Freeze on anomaly: the pipeline calls `update()` only for windows it judged
  normal, so an incident is never learned as the new normal.
* Std floor: a perfectly quiet system has std≈0, which would turn one stray
  error into z=1000. The floor keeps z meaningful.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class EWMABaseline:
    alpha: float = 0.05
    warmup: int = 12
    min_std: float = 0.01
    mean: float = 0.0
    var: float = 0.0
    n: int = 0

    @property
    def ready(self) -> bool:
        return self.n >= self.warmup

    @property
    def std(self) -> float:
        return max(math.sqrt(max(self.var, 0.0)), self.min_std)

    def update(self, x: float) -> None:
        if self.n == 0:
            self.mean, self.var = x, 0.0
        elif self.n < self.warmup:
            # plain running mean/var during warm-up for a faster, unbiased start
            k = self.n + 1
            delta = x - self.mean
            self.mean += delta / k
            self.var += (delta * (x - self.mean) - self.var) / k
        else:
            delta = x - self.mean
            self.mean += self.alpha * delta
            self.var = (1 - self.alpha) * (self.var + self.alpha * delta * delta)
        self.n += 1

    def zscore(self, x: float) -> float | None:
        if not self.ready:
            return None
        return (x - self.mean) / self.std

    def to_dict(self) -> dict:
        return {"mean": self.mean, "std": self.std, "n": self.n, "ready": self.ready}
