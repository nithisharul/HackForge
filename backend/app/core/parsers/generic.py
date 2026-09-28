"""Parser for the common "<timestamp> <LEVEL> [source] message" shape.

Accepted examples:
    2026-09-28T13:34:00.123Z ERROR [payments] Connection to 10.0.0.5 failed
    2026-09-28 13:34:00,123 WARN  Slow query took 812ms
    2026-09-28T13:34:00+05:30 INFO user 42 logged in
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.core.parsers.base import LogParser, normalize_level
from app.models.schemas import LogEvent

_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+"
    r"(?P<level>[A-Za-z]+)\s+"
    r"(?:\[(?P<source>[^\]]+)\]\s+)?"
    r"(?P<msg>.*)$"
)


def _parse_ts(text: str) -> datetime:
    text = text.replace(",", ".").replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


class GenericParser(LogParser):
    name = "generic"

    def parse(self, line: str) -> LogEvent | None:
        line = line.rstrip("\r\n")
        if not line.strip():
            return None
        m = _LINE_RE.match(line)
        if not m:
            # Unstructured line (stack-trace continuation etc.). Keep it as INFO
            # so volume stays honest, but never guess an error level.
            return LogEvent(
                timestamp=datetime.now(timezone.utc), level="INFO",
                message=line.strip(), raw=line,
            )
        try:
            ts = _parse_ts(m.group("ts"))
        except ValueError:
            ts = datetime.now(timezone.utc)
        return LogEvent(
            timestamp=ts,
            level=normalize_level(m.group("level")),
            message=m.group("msg").strip(),
            source=m.group("source"),
            raw=line,
        )
