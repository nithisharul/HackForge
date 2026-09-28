"""Feature engineering shared by training, evaluation and the live pipeline.

Keeping one implementation is the single most important thing for ML in
production: if training and serving compute features differently, the model
silently breaks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from app.core.parsers import get_parser
from app.core.template_miner import TemplateMiner
from app.core.window import SlidingWindow
from app.models.schemas import FEATURE_NAMES, WindowSnapshot

# columns that are counts get log1p so a 10x volume change is a fixed step
_LOG_COLS = [0, 1, 4, 5]


def transform_raw(X: np.ndarray) -> np.ndarray:
    X = np.asarray(X, dtype=np.float64).copy()
    if X.ndim == 1:
        X = X[None, :]
    X[:, _LOG_COLS] = np.log1p(np.clip(X[:, _LOG_COLS], 0, None))
    return X


@dataclass
class FeatureScaler:
    mean: np.ndarray
    std: np.ndarray

    @classmethod
    def fit(cls, X_raw: np.ndarray) -> "FeatureScaler":
        T = transform_raw(X_raw)
        return cls(T.mean(axis=0), np.maximum(T.std(axis=0), 1e-3))

    def transform(self, X_raw: np.ndarray) -> np.ndarray:
        return (transform_raw(X_raw) - self.mean) / self.std

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({
            "features": FEATURE_NAMES, "mean": self.mean.tolist(), "std": self.std.tolist(),
        }, indent=2))

    @classmethod
    def load(cls, path: Path) -> "FeatureScaler":
        d = json.loads(path.read_text())
        return cls(np.array(d["mean"]), np.array(d["std"]))


def make_sequences(X: np.ndarray, length: int) -> np.ndarray:
    """(n, f) -> (n-length+1, length, f) rolling sequences."""
    if len(X) < length:
        return np.empty((0, length, X.shape[1]))
    idx = np.arange(length)[None, :] + np.arange(len(X) - length + 1)[:, None]
    return X[idx]


def featurize_lines(lines: Iterable[str], log_format: str = "generic",
                    window_seconds: float = 60.0, tick_seconds: float = 5.0,
                    min_events: int = 20) -> list[WindowSnapshot]:
    """Replay a log offline using *log timestamps* and return one snapshot per tick.

    Uses exactly the same parser / template miner / window as the live pipeline.
    """
    parser = get_parser(log_format)
    miner = TemplateMiner()
    window = SlidingWindow(window_seconds, min_events)
    snaps: list[WindowSnapshot] = []
    next_tick: float | None = None
    for line in lines:
        ev = parser.parse(line)
        if ev is None:
            continue
        ts = ev.timestamp.timestamp()
        if next_tick is None:
            next_tick = ts + tick_seconds
        while ts >= next_tick:
            snaps.append(window.snapshot(next_tick, miner.names))
            next_tick += tick_seconds
        tid, _tpl, is_new = miner.add(ev.message)
        window.add(ts, ev.is_error, ev.is_warn, tid, is_new, ev.raw)
    if next_tick is not None:
        snaps.append(window.snapshot(next_tick, miner.names))
    return snaps


def snapshots_to_matrix(snaps: list[WindowSnapshot], skip_warmup: int = 0,
                        drop_low_confidence: bool = True) -> np.ndarray:
    rows = [s.vector() for s in snaps[skip_warmup:]
            if not (drop_low_confidence and s.low_confidence)]
    return np.array(rows, dtype=np.float64).reshape(-1, len(FEATURE_NAMES))
