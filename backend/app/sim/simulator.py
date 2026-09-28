"""Synthetic log generator with injectable anomaly scenarios.

Used by:
  * scripts/generate_logs.py         - live stream into data/app.log
  * scripts/generate_training_data.py - offline normal + labelled test logs
  * the built-in demo simulator and POST /demo/inject-anomaly

Normal traffic: ~`rate` lines/s (Poisson, with a gentle sine wobble), about
3% ERROR and 7% WARN spread across realistic message templates.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone

SERVICES = ["api", "auth", "payments", "orders", "search", "worker"]

INFO_MSGS = [
    lambda r: f"GET /api/v1/orders/{r.randint(1000, 99999)} 200 {r.randint(5, 180)}ms",
    lambda r: f"POST /api/v1/checkout 201 {r.randint(40, 300)}ms",
    lambda r: f"User {r.randint(1, 50000)} logged in from 10.0.{r.randint(0, 255)}.{r.randint(1, 254)}",
    lambda r: f"Cache hit ratio {r.uniform(0.85, 0.99):.2f} for key space sessions",
    lambda r: f"Processed batch {r.randint(1, 9999)} with {r.randint(10, 500)} records",
    lambda r: f"Health check ok latency={r.randint(1, 20)}ms",
    lambda r: f"Published event order.created id={r.randint(100000, 999999)}",
    lambda r: f"Scheduled job cleanup finished in {r.randint(100, 900)}ms",
]
WARN_MSGS = [
    lambda r: f"Slow query took {r.randint(500, 2000)}ms on table orders",
    lambda r: f"Retrying request to inventory-service attempt {r.randint(1, 3)}",
    lambda r: f"Connection pool at {r.randint(70, 90)}% capacity",
    lambda r: f"Deprecated API version v1 used by client {r.randint(1, 300)}",
]
ERROR_MSGS = [
    lambda r: f"Timeout calling payment-gateway after {r.randint(3000, 5000)}ms",
    lambda r: f"Failed to parse request body: unexpected token at position {r.randint(1, 400)}",
    lambda r: f"User {r.randint(1, 50000)} authentication failed: invalid token",
]
# Errors that never occur in normal operation (for the "new error type" scenario)
NOVEL_ERRORS = [
    lambda r: f"OutOfMemoryError: Java heap space in worker-{r.randint(1, 8)}",
    lambda r: f"Connection refused to db-primary 10.0.5.{r.randint(10, 20)}:5432",
    lambda r: f"Disk quota exceeded on /var/lib/data volume {r.randint(1, 4)}",
    lambda r: f"Circuit breaker OPEN for payment-gateway after {r.randint(20, 60)} failures",
]

SCENARIOS = {
    "error_spike":  "Error rate jumps to ~40% (e.g. a bad deploy)",
    "slow_ramp":    "Error rate climbs gradually from 3% to ~25%",
    "volume_surge": "Traffic x4 with a normal error rate (e.g. bot traffic / retry storm)",
    "volume_drop":  "Traffic drops to ~15% (a service going quiet)",
    "new_errors":   "Never-seen-before error messages at a moderate rate",
    "fatal_burst":  "Error rate ~75% with FATAL database errors",
}


@dataclass
class Anomaly:
    kind: str
    start: float        # unix seconds
    duration: float

    def active(self, t: float) -> bool:
        return self.start <= t < self.start + self.duration

    def progress(self, t: float) -> float:
        return min(max((t - self.start) / self.duration, 0.0), 1.0)


class LogSimulator:
    def __init__(self, rate: float = 20.0, error_rate: float = 0.03, warn_rate: float = 0.07,
                 seed: int | None = None):
        self.rate = rate
        self.error_rate = error_rate
        self.warn_rate = warn_rate
        self.rng = random.Random(seed)
        self.anomalies: list[Anomaly] = []

    def inject(self, kind: str, start: float, duration: float) -> Anomaly:
        if kind not in SCENARIOS:
            raise ValueError(f"unknown scenario {kind}; options: {list(SCENARIOS)}")
        a = Anomaly(kind, start, duration)
        self.anomalies.append(a)
        return a

    def _params(self, t: float) -> tuple[float, float, float, str | None]:
        """(lines/sec, error_rate, novel_share, active_kind) at time t."""
        rate = self.rate * (1 + 0.15 * math.sin(t / 600 * 2 * math.pi))
        err, novel, kind = self.error_rate, 0.0, None
        for a in self.anomalies:
            if not a.active(t):
                continue
            kind = a.kind
            if a.kind == "error_spike":
                err = 0.40
            elif a.kind == "slow_ramp":
                err = self.error_rate + (0.25 - self.error_rate) * a.progress(t)
            elif a.kind == "volume_surge":
                rate *= 4
            elif a.kind == "volume_drop":
                rate *= 0.15
            elif a.kind == "new_errors":
                err, novel = 0.10, 0.8
            elif a.kind == "fatal_burst":
                err, novel = 0.75, 0.5
        return rate, err, novel, kind

    def line(self, t: float) -> str:
        r = self.rng
        _rate, err, novel, kind = self._params(t)
        ts = datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        svc = r.choice(SERVICES)
        x = r.random()
        if x < err:
            if r.random() < novel:
                level = "FATAL" if kind == "fatal_burst" and r.random() < 0.3 else "ERROR"
                msg = r.choice(NOVEL_ERRORS)(r)
            else:
                level, msg = "ERROR", r.choice(ERROR_MSGS)(r)
        elif x < err + self.warn_rate:
            level, msg = "WARN", r.choice(WARN_MSGS)(r)
        else:
            level, msg = "INFO", r.choice(INFO_MSGS)(r)
        return f"{ts} {level} [{svc}] {msg}"

    def generate(self, start: float, duration: float):
        """Offline: yield (t, line) with Poisson arrivals between start and start+duration."""
        t = start
        end = start + duration
        while True:
            rate = self._params(t)[0]
            t += self.rng.expovariate(max(rate, 0.01))
            if t >= end:
                return
            yield t, self.line(t)

    def lines_for_interval(self, t0: float, t1: float) -> list[str]:
        """Live: lines for the wall-clock slice [t0, t1)."""
        rate = self._params(t0)[0]
        n = _poisson(self.rng, rate * (t1 - t0))
        times = sorted(self.rng.uniform(t0, t1) for _ in range(n))
        return [self.line(t) for t in times]


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:  # normal approximation
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1
