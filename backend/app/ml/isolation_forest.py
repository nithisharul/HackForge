"""Isolation Forest over a single window's feature vector.

Idea: build random trees that split features at random values. Anomalous
points are "easy to isolate" and end up close to the root, so their average
path length is short. sklearn's score_samples is higher for normal points, so
we negate it.
"""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from app.ml.base import AnomalyModel

FILE = "iforest.joblib"


class IsolationForestModel(AnomalyModel):
    name = "iforest"
    sequence_length = 1

    def __init__(self, n_estimators: int = 200, random_state: int = 42):
        self.model = IsolationForest(
            n_estimators=n_estimators, contamination="auto", random_state=random_state,
        )

    def fit(self, X: np.ndarray) -> "IsolationForestModel":
        self.model.fit(X)
        return self

    def score(self, X: np.ndarray) -> np.ndarray:
        return -self.model.score_samples(np.atleast_2d(X))

    def save(self, directory: Path) -> None:
        joblib.dump(self.model, directory / FILE)

    @classmethod
    def load(cls, directory: Path) -> "IsolationForestModel":
        obj = cls.__new__(cls)
        obj.model = joblib.load(directory / FILE)
        return obj

    @staticmethod
    def exists(directory: Path) -> bool:
        return (directory / FILE).exists()
