"""Decides *when* to emit an alert so one incident != fifty notifications.

State machine per detector stream:

    NORMAL --(severity > NONE)--> ACTIVE     emit OPEN
    ACTIVE --(severity rises)---> ACTIVE     emit ESCALATED   (ignores cooldown)
    ACTIVE --(cooldown expired)-> ACTIVE     emit OPEN reminder
    ACTIVE --(N normal windows)-> NORMAL     emit RESOLVED
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.models.schemas import Severity

Action = Literal["OPEN", "ESCALATED", "REMINDER", "RESOLVED"]


@dataclass
class AlertDecision:
    action: Action
    severity: Severity
    consecutive: int
    peak: Severity


class AlertManager:
    def __init__(self, cooldown_seconds: float = 60.0, resolve_after: int = 3):
        self.cooldown = cooldown_seconds
        self.resolve_after = resolve_after
        self.active = False
        self.last_emitted_severity = Severity.NONE
        self.peak = Severity.NONE
        self.peak_at_last_alert = Severity.NONE
        self.last_alert_ts = 0.0
        self.normal_streak = 0
        self.consecutive = 0          # consecutive anomalous windows
        self.suppressed = 0           # alerts swallowed by dedup/cooldown

    def process(self, severity: Severity, now: float, consecutive: int = 0) -> AlertDecision | None:
        self.consecutive = consecutive
        if severity > Severity.NONE:
            self.normal_streak = 0
            self.peak = max(self.peak, severity)
            if not self.active:
                self.active = True
                return self._emit("OPEN", severity, now)
            if severity > self.peak_at_last_alert:
                # only a new high for this episode counts as escalation; wobbling
                # back up after a lower reminder does not
                return self._emit("ESCALATED", severity, now)
            if now - self.last_alert_ts >= self.cooldown:
                return self._emit("REMINDER", severity, now)
            self.suppressed += 1
            return None

        if self.active:
            self.normal_streak += 1
            if self.normal_streak >= self.resolve_after:
                decision = AlertDecision("RESOLVED", Severity.NONE, 0, self.peak)
                self.active = False
                self.last_emitted_severity = Severity.NONE
                self.peak = Severity.NONE
                self.peak_at_last_alert = Severity.NONE
                self.normal_streak = 0
                self.last_alert_ts = now
                return decision
        return None

    def _emit(self, action: Action, severity: Severity, now: float) -> AlertDecision:
        self.last_emitted_severity = severity
        self.peak_at_last_alert = max(self.peak_at_last_alert, severity)
        self.last_alert_ts = now
        return AlertDecision(action, severity, self.consecutive, self.peak)
