"""Groups alerts into incidents.

Every alert episode (OPEN ... RESOLVED) belongs to an incident. If a new episode
starts within `gap_seconds` of the previous incident resolving, it is treated as
the same flapping incident instead of a brand new one.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from app.models.schemas import Incident, Severity


class IncidentGrouper:
    def __init__(self, gap_seconds: float = 120.0):
        self.gap = gap_seconds
        self.current: Incident | None = None

    def assign(self, action: str, severity: Severity, ts: datetime, title: str) -> Incident:
        cur = self.current
        reopen = (
            cur is not None and cur.status == "RESOLVED" and cur.resolved_at is not None
            and (ts - cur.resolved_at).total_seconds() <= self.gap
        )
        if action == "OPEN" and (cur is None or (cur.status == "RESOLVED" and not reopen)):
            cur = Incident(
                id=f"INC-{ts:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}",
                started_at=ts, last_seen=ts, peak_severity=severity.name,
                alert_count=0, title=title,
            )
            self.current = cur
        elif action == "OPEN" and reopen:
            cur.status = "OPEN"
            cur.resolved_at = None
        assert cur is not None
        cur.alert_count += 1
        cur.last_seen = ts
        if severity > Severity.parse(cur.peak_severity):
            cur.peak_severity = severity.name
            cur.title = title
        if action == "RESOLVED":
            cur.status = "RESOLVED"
            cur.resolved_at = ts
        return cur
