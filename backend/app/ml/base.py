"""Interface every anomaly model implements.

`score()` returns a raw score where HIGHER means MORE anomalous. Raw scores are
not comparable across models; `calibration.py` turns them into a common scale.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np


class AnomalyModel(ABC):
    name: str = "base"
    #: number of consecutive windows the model needs (1 = single window)
    sequence_length: int = 1

    @abstractmethod
    def fit(self, X: np.ndarray) -> "AnomalyModel":
        """X is scaled features: (n, f) or (n, seq, f) for sequence models."""

    @abstractmethod
    def score(self, X: np.ndarray) -> np.ndarray:
        """Raw anomaly score per row. Higher = more anomalous."""

    def feature_contributions(self, X: np.ndarray) -> np.ndarray | None:
        """Optional per-feature contribution for the LAST row (for explanations)."""
        return None

    @abstractmethod
    def save(self, directory: Path) -> None: ...

    @classmethod
    @abstractmethod
    def load(cls, directory: Path) -> "AnomalyModel": ...
