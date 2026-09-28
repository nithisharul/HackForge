"""Parser interface: raw line -> LogEvent (or None if the line is unusable)."""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.schemas import LogEvent

LEVEL_ALIASES = {
    "WARNING": "WARN",
    "ERR": "ERROR",
    "SEVERE": "ERROR",
    "CRIT": "FATAL",
    "CRITICAL": "FATAL",
    "FAILURE": "FATAL",
    "SEVERE_ERROR": "ERROR",
    "TRACE": "DEBUG",
}


def normalize_level(level: str) -> str:
    level = level.strip().upper()
    return LEVEL_ALIASES.get(level, level)


class LogParser(ABC):
    name: str = "base"

    @abstractmethod
    def parse(self, line: str) -> LogEvent | None:
        ...
