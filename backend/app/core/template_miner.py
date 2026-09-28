"""Turns a log message into a template ID by masking the variable parts.

    "Connection to 10.0.0.5:5432 failed after 3012ms"
 -> "Connection to <IP> failed after <NUM>ms"

This is a light, dependency-free version of what Drain3 does. It is deterministic
and fast (~1µs per line) which matters more here than perfect clustering. The
miner also remembers how often each template has been seen so the root-cause
module can point at *new* or *rare* templates during an anomaly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_MASKS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<HEX>"),
    (re.compile(r"(?<=[=:])\s*\S+@\S+"), "<EMAIL>"),
    (re.compile(r"\"[^\"]*\"|'[^']*'"), "<STR>"),
    (re.compile(r"(?:/[\w.\-]+){2,}"), "<PATH>"),
    (re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?"), "<NUM>"),
]
_WS = re.compile(r"\s+")


def to_template(message: str) -> str:
    text = message
    for pattern, token in _MASKS:
        text = pattern.sub(token, text)
    return _WS.sub(" ", text).strip()[:300]


@dataclass
class TemplateMiner:
    templates: dict[str, int] = field(default_factory=dict)   # template -> id
    counts: dict[int, int] = field(default_factory=dict)      # id -> times seen
    names: dict[int, str] = field(default_factory=dict)       # id -> template
    total: int = 0

    def add(self, message: str) -> tuple[int, str, bool]:
        """Returns (template_id, template, is_new)."""
        template = to_template(message)
        tid = self.templates.get(template)
        is_new = tid is None
        if is_new:
            tid = len(self.templates) + 1
            self.templates[template] = tid
            self.names[tid] = template
            self.counts[tid] = 0
        self.counts[tid] += 1
        self.total += 1
        return tid, template, is_new

    def frequency(self, tid: int) -> float:
        return self.counts.get(tid, 0) / self.total if self.total else 0.0

    def __len__(self) -> int:
        return len(self.templates)
