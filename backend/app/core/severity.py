"""Maps a normalised anomaly score to a severity level.

All detectors are normalised so that 1.0 is the edge of normal (z = 3 for the
statistical detector, the 99th percentile of normal data for ML models). So a
score of 2.0 means "twice as far out as the normal boundary".

Overrides:
  1. Absolute error rate >= critical_error_rate (default 50%) is CRITICAL,
     whatever the baseline says. Protects systems that are always noisy.
  2. An anomaly that persists for `escalate_after` consecutive windows is
     escalated one level (a sustained MEDIUM is worse than a blip).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import Severity


@dataclass
class SeverityPolicy:
    low: float = 1.0
    medium: float = 1.5
    high: float = 2.5
    critical: float = 4.0
    critical_error_rate: float = 0.5
    escalate_after: int = 3

    def from_score(self, score: float) -> Severity:
        if score >= self.critical:
            return Severity.CRITICAL
        if score >= self.high:
            return Severity.HIGH
        if score >= self.medium:
            return Severity.MEDIUM
        if score >= self.low:
            return Severity.LOW
        return Severity.NONE

    def classify(self, score: float, error_rate: float, consecutive: int,
                 low_confidence: bool = False) -> tuple[Severity, list[str]]:
        """Returns (severity, reasons)."""
        reasons: list[str] = []
        sev = self.from_score(score)
        if sev > Severity.NONE:
            reasons.append(f"anomaly score {score:.2f} (1.0 = edge of normal)")
        if not low_confidence and error_rate >= self.critical_error_rate:
            if sev < Severity.CRITICAL:
                reasons.append(f"error rate {error_rate:.0%} >= {self.critical_error_rate:.0%} absolute limit")
            sev = Severity.CRITICAL
        if sev > Severity.NONE and consecutive >= self.escalate_after and sev < Severity.CRITICAL:
            sev = Severity(sev + 1)
            reasons.append(f"persisted for {consecutive} consecutive windows (escalated)")
        return sev, reasons
