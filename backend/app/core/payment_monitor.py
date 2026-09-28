"""Payment-scoped detector: checkout failure rate in the rolling window.

The global detector scores all lines together, so a failing checkout path that
is ~2-3% of traffic barely moves the overall error rate. This monitor reads
ONLY explicit checkout request completion lines from the payments service

    2026-09-28T10:00:00.000Z ERROR [payments] POST /api/v1/checkout 503 3120ms request_id=...

and counts a request as failed iff its HTTP status is 5xx. The log level is
ignored, so other ERROR lines from [payments] (timeouts, retries...) are not
counted as extra failed requests. It is evaluated every tick, independently of
what the global detector says.

    NORMAL ----(>= min requests, failure rate >= threshold)----> OPEN          alert OPEN
    OPEN/RECOVERING --(>= min requests, rate < healthy)--------> RECOVERING    healthy window k/N
    RECOVERING --(N consecutive healthy windows)---------------> NORMAL        alert RESOLVED
    OPEN/RECOVERING --(< min requests)--> OPEN, recovery unverified, streak reset
    OPEN/RECOVERING --(rate >= healthy)--> OPEN, streak reset

No repeat alerts while an incident stays open; phase changes are reported as
"UPDATE" so the pipeline can refresh the stored incident without a new alert.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from app.models.schemas import LogEvent

_REQUEST_RE = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE)\s+(?P<path>/\S*)\s+(?P<status>[1-5]\d\d)\b")


@dataclass(slots=True)
class _Request:
    ts: float
    failed: bool
    raw: str


@dataclass
class PaymentEvaluation:
    snapshot: dict
    action: str | None = None          # OPEN | RESOLVED | UPDATE | None
    evidence: list[str] = field(default_factory=list)


class PaymentMonitor:
    def __init__(self, window_seconds: float = 60.0, source: str = "payments",
                 route: str = "/api/v1/checkout", failure_threshold: float = 0.20,
                 healthy_rate: float = 0.05, min_requests: int = 20, resolve_after: int = 3,
                 evidence_lines: int = 5, max_requests: int = 20000):
        self.window_seconds = window_seconds
        self.source = source
        self.route = route.rstrip("/")
        self.failure_threshold = failure_threshold
        self.healthy_rate = healthy_rate
        self.min_requests = min_requests
        self.resolve_after = resolve_after
        self.evidence_lines = evidence_lines
        self.max_requests = max_requests
        self._reqs: deque[_Request] = deque()
        self._failed = 0
        self.phase = "NORMAL"            # NORMAL | OPEN | RECOVERING
        self.healthy_streak = 0
        self.unverified = False          # open incident but too little traffic to judge recovery
        self.peak_rate = 0.0
        self.evidence: list[str] = []
        self._last_state: tuple | None = None

    # ---- input -----------------------------------------------------------------
    def observe(self, ts: float, ev: LogEvent) -> bool:
        """Record one parsed line; returns True if it was a checkout completion."""
        if ev.source != self.source:
            return False
        m = _REQUEST_RE.match(ev.message)
        if not m:
            return False
        path = m.group("path").split("?", 1)[0].rstrip("/")
        if path != self.route:
            return False
        failed = m.group("status").startswith("5")
        self._reqs.append(_Request(ts, failed, ev.raw or ev.message))
        self._failed += failed
        if len(self._reqs) > self.max_requests:      # hard memory bound
            self._failed -= self._reqs.popleft().failed
        return True

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._reqs and self._reqs[0].ts < cutoff:
            self._failed -= self._reqs.popleft().failed

    def _recent(self, failed: bool, k: int) -> list[str]:
        out = []
        for r in reversed(self._reqs):
            if r.failed == failed:
                out.append(r.raw)
                if len(out) >= k:
                    break
        return out[::-1]

    # ---- one tick --------------------------------------------------------------
    def snapshot(self) -> dict:
        n = len(self._reqs)
        return {
            "scope": "payments", "route": self.route,
            "requests": n, "failures": self._failed,
            "failure_rate": round(self._failed / n, 4) if n else 0.0,
            "window_seconds": self.window_seconds,
            "enough_traffic": n >= self.min_requests, "min_requests": self.min_requests,
            "threshold": self.failure_threshold, "healthy_rate": self.healthy_rate,
            "phase": self.phase, "healthy_windows": self.healthy_streak,
            "resolve_after": self.resolve_after, "recovery_unverified": self.unverified,
            "peak_failure_rate": round(self.peak_rate, 4),
        }

    def evaluate(self, now: float) -> PaymentEvaluation:
        self._evict(now)
        n = len(self._reqs)
        rate = self._failed / n if n else 0.0
        enough = n >= self.min_requests
        action = None

        if self.phase == "NORMAL":
            if enough and rate >= self.failure_threshold:
                self.phase, self.healthy_streak, self.unverified = "OPEN", 0, False
                self.peak_rate = rate
                self.evidence = self._recent(True, self.evidence_lines)
                action = "OPEN"
        else:
            self.peak_rate = max(self.peak_rate, rate) if enough else self.peak_rate
            if not enough:
                # silence is not success: keep the incident open, restart the count
                self.phase, self.healthy_streak, self.unverified = "OPEN", 0, True
            elif rate < self.healthy_rate:
                self.phase, self.unverified = "RECOVERING", False
                self.healthy_streak += 1
                if self.healthy_streak >= self.resolve_after:
                    self.evidence = self._recent(False, self.evidence_lines)
                    action = "RESOLVED"
            else:
                self.phase, self.healthy_streak, self.unverified = "OPEN", 0, False

        snap = self.snapshot()
        if action == "RESOLVED":
            snap["phase"] = "RESOLVED"
            self.phase, self.healthy_streak, self.peak_rate = "NORMAL", 0, 0.0
        # a new peak also refreshes the stored incident (still no new alert)
        state = (self.phase, self.healthy_streak, self.unverified, round(self.peak_rate, 2))
        if action is None and self.phase != "NORMAL" and state != self._last_state:
            action = "UPDATE"
        self._last_state = state
        return PaymentEvaluation(snap, action, list(self.evidence) if action else [])
