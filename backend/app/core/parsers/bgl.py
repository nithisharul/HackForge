"""Parser for the public BGL (Blue Gene/L) supercomputer log dataset (LogHub).

Line layout:
    <label> <unix_ts> <date> <node> <time> <node_repeat> RAS <component> <LEVEL> <message>
label is "-" for normal lines and an alert category (e.g. KERNDTLB) otherwise.
The label is kept in `source` as "label=<x>" so benchmarks can use ground truth.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.parsers.base import LogParser, normalize_level
from app.models.schemas import LogEvent


class BGLParser(LogParser):
    name = "bgl"

    def parse(self, line: str) -> LogEvent | None:
        parts = line.rstrip("\n").split(maxsplit=9)
        if len(parts) < 10:
            return None
        label, unix_ts, *_rest = parts
        try:
            ts = datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)
        except ValueError:
            return None
        level = normalize_level(parts[8])
        return LogEvent(
            timestamp=ts,
            level=level,
            message=parts[9],
            source=f"label={label}",
            raw=line.rstrip("\n"),
        )
