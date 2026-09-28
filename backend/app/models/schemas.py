"""Pydantic schemas shared by the pipeline, the API and the publishers."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Severity(IntEnum):
    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, value: str | int | "Severity") -> "Severity":
        if isinstance(value, Severity):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[value.upper()]


class LogEvent(BaseModel):
    timestamp: datetime
    level: str                      # DEBUG / INFO / WARN / ERROR / FATAL
    message: str
    source: str | None = None
    template_id: int | None = None
    template: str | None = None
    raw: str = ""

    @property
    def is_error(self) -> bool:
        return self.level in ("ERROR", "FATAL", "CRITICAL")

    @property
    def is_warn(self) -> bool:
        return self.level in ("WARN", "WARNING")


FEATURE_NAMES = [
    "volume",            # events in window
    "error_count",
    "error_rate",
    "warn_rate",
    "unique_templates",
    "new_templates",     # templates never seen before this window
]


class WindowSnapshot(BaseModel):
    """Feature vector for one evaluation tick."""
    ts: datetime
    window_seconds: float
    volume: int
    error_count: int
    warn_count: int
    error_rate: float
    warn_rate: float
    unique_templates: int
    new_templates: int
    low_confidence: bool
    top_templates: list[dict[str, Any]] = Field(default_factory=list)

    def vector(self) -> list[float]:
        return [
            float(self.volume), float(self.error_count), self.error_rate,
            self.warn_rate, float(self.unique_templates), float(self.new_templates),
        ]


class DetectorScore(BaseModel):
    name: str
    raw: float
    normalized: float                # 1.0 == edge of normal
    available: bool = True


class Metrics(BaseModel):
    """What the dashboard charts every tick."""
    ts: datetime
    volume: int
    error_rate: float
    warn_rate: float
    baseline_mean: float | None
    baseline_std: float | None
    baseline_ready: bool
    zscore: float | None
    final_score: float
    severity: str
    low_confidence: bool
    detectors: list[DetectorScore] = Field(default_factory=list)
    payment: dict[str, Any] | None = None    # payment-scoped window stats (separate from the fields above)


class Alert(BaseModel):
    id: str
    incident_id: str | None = None
    ts: datetime = Field(default_factory=utcnow)
    status: Literal["OPEN", "ESCALATED", "RESOLVED"] = "OPEN"
    severity: str
    title: str
    description: str
    error_rate: float
    baseline_mean: float | None = None
    zscore: float | None = None
    final_score: float
    consecutive_windows: int = 1
    detectors: list[DetectorScore] = Field(default_factory=list)
    top_features: list[dict[str, Any]] = Field(default_factory=list)
    root_cause: list[dict[str, Any]] = Field(default_factory=list)
    sample_lines: list[str] = Field(default_factory=list)
    delivered_to: list[str] = Field(default_factory=list)
    scope: str = "global"            # "global" detector or a scoped one such as "payments"


class Incident(BaseModel):
    id: str
    started_at: datetime
    last_seen: datetime
    resolved_at: datetime | None = None
    peak_severity: str
    alert_count: int
    status: Literal["OPEN", "RESOLVED"] = "OPEN"
    title: str
    scope: str = "global"
    details: dict[str, Any] = Field(default_factory=dict)   # scoped detectors: phase, stats, evidence
