"""Appends alerts as JSON lines to a local file.

Used (a) as a destination when AWS is disabled and (b) as the fallback when an
AWS publisher gives up after its retries, so no alert is ever silently lost.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.models.schemas import Alert, Severity
from app.publishers.base import Publisher


class LocalFilePublisher(Publisher):
    name = "local_file"
    min_severity = Severity.NONE

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, alert: Alert, note: str | None) -> None:
        record = alert.model_dump(mode="json")
        if note:
            record["_fallback_reason"] = note
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    async def publish(self, alert: Alert, note: str | None = None) -> None:
        await asyncio.to_thread(self._write, alert, note)
