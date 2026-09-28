"""The decision core: WindowSnapshot -> scores -> severity.

Pure and synchronous (no I/O), so the live pipeline, the offline evaluator and
the unit tests all run exactly the same logic.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from app.core.baseline import EWMABaseline
from app.core.severity import SeverityPolicy
from app.ml.calibration import normalize_z
from app.ml.ensemble import blend
from app.ml.registry import ModelBundle
from app.models.schemas import FEATURE_NAMES, DetectorScore, Severity, WindowSnapshot

FEATURE_LABELS = {
    "volume": "log volume",
    "error_count": "error count",
    "error_rate": "error rate",
    "warn_rate": "warning rate",
    "unique_templates": "distinct message types",
    "new_templates": "never-seen message types",
}


@dataclass
class Evaluation:
    snapshot: WindowSnapshot
    zscore: float | None
    detectors: list[DetectorScore]
    final_score: float
    severity: Severity
    reasons: list[str]
    consecutive: int
    baseline_mean: float | None
    baseline_std: float | None
    baseline_ready: bool
    top_features: list[dict] = field(default_factory=list)

    @property
    def is_anomalous(self) -> bool:
        return self.severity > Severity.NONE


class Detector:
    def __init__(self, baseline: EWMABaseline, policy: SeverityPolicy,
                 bundle: ModelBundle | None = None, weights: dict[str, float] | None = None,
                 sequence_length: int = 10, ml_warmup_ticks: int = 12):
        self.baseline = baseline
        # The first window after startup is full of "new" templates (the miner has
        # never seen anything yet), so ML models skip it instead of alerting on it.
        self.ml_warmup_ticks = ml_warmup_ticks
        self.ticks = 0
        self.policy = policy
        self.bundle = bundle or ModelBundle()
        self.weights = weights or {}
        self.history: deque[np.ndarray] = deque(maxlen=sequence_length)
        self.consecutive = 0

    def set_bundle(self, bundle: ModelBundle) -> None:
        self.bundle = bundle
        seq = max((m.sequence_length for m in bundle.models.values()), default=1)
        self.history = deque(self.history, maxlen=max(seq, 1))

    # ------------------------------------------------------------------------
    def evaluate(self, snap: WindowSnapshot) -> Evaluation:
        detectors: list[DetectorScore] = []
        self.ticks += 1

        # 1) statistical detector on error rate
        z = None if snap.low_confidence else self.baseline.zscore(snap.error_rate)
        detectors.append(DetectorScore(
            name="zscore", raw=round(z, 4) if z is not None else 0.0,
            normalized=round(normalize_z(z), 4), available=z is not None,
        ))

        # 2) ML models on the full feature vector
        contributions = None
        scaled = None
        if self.bundle.ready and self.ticks > self.ml_warmup_ticks:
            scaled = self.bundle.scaler.transform(np.array(snap.vector()))[0]
            self.history.append(scaled)
            for name, model in self.bundle.models.items():
                cal = self.bundle.calibrations.get(name)
                if model.sequence_length > 1:
                    if len(self.history) < model.sequence_length:
                        detectors.append(DetectorScore(name=name, raw=0, normalized=0, available=False))
                        continue
                    X = np.stack(list(self.history)[-model.sequence_length:])[None]
                else:
                    X = scaled[None]
                raw = float(model.score(X)[0])
                norm = cal.normalize(raw) if cal else 0.0
                detectors.append(DetectorScore(name=name, raw=round(raw, 6),
                                               normalized=round(float(norm), 4)))
                c = model.feature_contributions(X)
                if c is not None:
                    contributions = c

        final = blend(detectors, self.weights)

        # 3) severity (warm-up => never alert)
        tentative = self.policy.from_score(final) > Severity.NONE or (
            not snap.low_confidence and snap.error_rate >= self.policy.critical_error_rate)
        consecutive = self.consecutive + 1 if tentative else 0
        if self.baseline.ready:
            severity, reasons = self.policy.classify(final, snap.error_rate, consecutive,
                                                     snap.low_confidence)
        else:
            severity, reasons = Severity.NONE, ["baseline warming up"]
        self.consecutive = consecutive if self.baseline.ready else 0

        # 4) learn only from normal, confident windows (freeze on anomaly)
        if not snap.low_confidence and (not self.baseline.ready or severity == Severity.NONE):
            self.baseline.update(snap.error_rate)

        top = self._explain(snap, scaled, contributions) if severity > Severity.NONE else []
        return Evaluation(
            snapshot=snap, zscore=z, detectors=detectors, final_score=round(final, 4),
            severity=severity, reasons=reasons, consecutive=self.consecutive,
            baseline_mean=round(self.baseline.mean, 6) if self.baseline.n else None,
            baseline_std=round(self.baseline.std, 6) if self.baseline.n else None,
            baseline_ready=self.baseline.ready, top_features=top,
        )

    # ------------------------------------------------------------------------
    def _explain(self, snap: WindowSnapshot, scaled, contributions) -> list[dict]:
        """Which features drove the score: LSTM-AE per-feature reconstruction error
        when available, otherwise distance from the training mean."""
        vec = snap.vector()
        if scaled is None:
            return [{"feature": "error_rate", "label": FEATURE_LABELS["error_rate"],
                     "value": snap.error_rate, "usual": round(self.baseline.mean, 4),
                     "share": 1.0}]
        if contributions is None:
            dev = np.abs(scaled)
            contributions = dev / dev.sum() if dev.sum() > 0 else dev
        scaler = self.bundle.scaler
        out = []
        for i in np.argsort(contributions)[::-1][:3]:
            name = FEATURE_NAMES[i]
            usual = scaler.mean[i]
            if i in (0, 1, 4, 5):
                usual = float(np.expm1(usual))
            direction = "above" if scaled[i] > 0 else "below"
            out.append({
                "feature": name, "label": FEATURE_LABELS[name],
                "value": round(vec[i], 4), "usual": round(float(usual), 4),
                "direction": direction, "share": round(float(contributions[i]), 3),
            })
        return out
