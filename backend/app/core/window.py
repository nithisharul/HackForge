"""Time-based sliding window over log events.

Events go in as they arrive; every tick `snapshot(now)` evicts everything older
than `window_seconds` and returns the feature vector for what is left. Time is
passed in explicitly so the exact same class is used for live data (wall clock)
and for offline training/benchmarks (log timestamps).
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models.schemas import WindowSnapshot


@dataclass(slots=True)
class _Entry:
    ts: float
    is_error: bool
    is_warn: bool
    template_id: int
    is_new_template: bool
    raw: str


class SlidingWindow:
    def __init__(self, window_seconds: float = 60.0, min_events: int = 20):
        self.window_seconds = window_seconds
        self.min_events = min_events
        self._events: deque[_Entry] = deque()
        # running counters so a snapshot is O(evicted) rather than O(window)
        self._errors = 0
        self._warns = 0
        self._new = 0
        self._templates: Counter[int] = Counter()

    def add(self, ts: float, is_error: bool, is_warn: bool, template_id: int,
            is_new_template: bool, raw: str = "") -> None:
        self._events.append(_Entry(ts, is_error, is_warn, template_id, is_new_template, raw))
        self._errors += is_error
        self._warns += is_warn
        self._new += is_new_template
        self._templates[template_id] += 1

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        ev = self._events
        while ev and ev[0].ts < cutoff:
            e = ev.popleft()
            self._errors -= e.is_error
            self._warns -= e.is_warn
            self._new -= e.is_new_template
            self._templates[e.template_id] -= 1
            if self._templates[e.template_id] <= 0:
                del self._templates[e.template_id]

    def __len__(self) -> int:
        return len(self._events)

    def snapshot(self, now: float, template_names: dict[int, str] | None = None) -> WindowSnapshot:
        self._evict(now)
        n = len(self._events)
        error_rate = self._errors / n if n else 0.0
        warn_rate = self._warns / n if n else 0.0
        top = []
        names = template_names or {}
        for tid, cnt in self._templates.most_common(5):
            top.append({"template_id": tid, "template": names.get(tid, str(tid)), "count": cnt})
        return WindowSnapshot(
            ts=datetime.fromtimestamp(now, tz=timezone.utc),
            window_seconds=self.window_seconds,
            volume=n,
            error_count=self._errors,
            warn_count=self._warns,
            error_rate=round(error_rate, 6),
            warn_rate=round(warn_rate, 6),
            unique_templates=len(self._templates),
            new_templates=self._new,
            low_confidence=n < self.min_events,
            top_templates=top,
        )

    def template_counts(self) -> Counter[int]:
        return Counter(self._templates)

    def error_template_counts(self) -> Counter[int]:
        return Counter(e.template_id for e in self._events if e.is_error)

    def recent_error_lines(self, k: int = 5) -> list[str]:
        out: list[str] = []
        for e in reversed(self._events):
            if e.is_error and e.raw:
                out.append(e.raw)
                if len(out) >= k:
                    break
        return out
