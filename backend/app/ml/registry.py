"""Loads trained artifacts (scaler, models, calibration) and supports hot reload."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.ml.base import AnomalyModel
from app.ml.calibration import Calibration, load_calibrations
from app.ml.features import FeatureScaler
from app.ml.isolation_forest import IsolationForestModel
from app.ml.lstm_autoencoder import LSTMAutoencoder

log = logging.getLogger(__name__)


@dataclass
class ModelBundle:
    scaler: FeatureScaler | None = None
    models: dict[str, AnomalyModel] = field(default_factory=dict)
    calibrations: dict[str, Calibration] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.scaler is not None and bool(self.models)


def load_bundle(directory: Path) -> ModelBundle:
    bundle = ModelBundle()
    if not (directory / "scaler.json").exists():
        log.warning("No trained artifacts in %s - running statistical detector only. "
                    "Run `python -m app.ml.train` to enable ML models.", directory)
        return bundle
    bundle.scaler = FeatureScaler.load(directory / "scaler.json")
    bundle.calibrations = load_calibrations(directory / "calibration.json")
    meta_path = directory / "metadata.json"
    bundle.metadata = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    for cls in (IsolationForestModel, LSTMAutoencoder):
        try:
            if cls.exists(directory) and cls.name in bundle.calibrations:
                bundle.models[cls.name] = cls.load(directory)
                log.info("Loaded model %s", cls.name)
        except Exception:  # a broken model must never stop the pipeline
            log.exception("Failed to load %s; continuing without it", cls.name)
    return bundle
