"""Publisher interface. A publisher delivers one alert to one destination."""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.schemas import Alert, Severity


class Publisher(ABC):
    name: str = "base"
    #: alerts below this severity are skipped (RESOLVED alerts carry the peak severity)
    min_severity: Severity = Severity.LOW

    async def start(self) -> None:
        """Optional one-time setup (create log group, check topic...)."""

    async def close(self) -> None:
        """Optional cleanup."""

    def wants(self, alert: Alert) -> bool:
        return Severity.parse(alert.severity) >= self.min_severity

    @abstractmethod
    async def publish(self, alert: Alert) -> None:
        """Deliver the alert. Raise on failure so the dispatcher can retry."""
