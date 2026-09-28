"""Fans each alert out to every publisher without letting one block another.

* Each publisher has its own asyncio.Queue and worker task, so a slow or
  unreachable AWS endpoint never delays the WebSocket feed.
* Failed deliveries are retried with exponential backoff (0.5s, 1s, 2s...).
* When a publisher gives up, the alert is written to the local fallback file
  with the reason, so nothing is lost.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.models.schemas import Alert
from app.publishers.base import Publisher
from app.publishers.local_file import LocalFilePublisher

log = logging.getLogger(__name__)


@dataclass
class PublisherStats:
    sent: int = 0
    failed: int = 0
    retried: int = 0
    skipped: int = 0
    last_error: str | None = None
    queue_depth: int = 0
    extra: dict = field(default_factory=dict)


class Dispatcher:
    def __init__(self, publishers: list[Publisher], fallback: LocalFilePublisher | None = None,
                 retries: int = 3, base_delay: float = 0.5, queue_size: int = 1000,
                 on_delivered=None):
        self.publishers = publishers
        self.on_delivered = on_delivered  # async callback(alert) after each success
        self.fallback = fallback
        self.retries = retries
        self.base_delay = base_delay
        self.queues: dict[str, asyncio.Queue] = {p.name: asyncio.Queue(queue_size) for p in publishers}
        self.stats: dict[str, PublisherStats] = {p.name: PublisherStats() for p in publishers}
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        for p in self.publishers:
            await p.start()
            self._tasks.append(asyncio.create_task(self._worker(p), name=f"publisher-{p.name}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for p in self.publishers:
            await p.close()

    def submit(self, alert: Alert) -> None:
        """Non-blocking: enqueue for every interested publisher."""
        for p in self.publishers:
            st = self.stats[p.name]
            if not p.wants(alert):
                st.skipped += 1
                continue
            try:
                self.queues[p.name].put_nowait(alert)
            except asyncio.QueueFull:
                st.failed += 1
                st.last_error = "queue full"
                log.error("Queue full for %s; dropping alert %s", p.name, alert.id)

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait until all queues are empty (used by tests and shutdown)."""
        await asyncio.wait_for(asyncio.gather(*(q.join() for q in self.queues.values())), timeout)

    async def _worker(self, p: Publisher) -> None:
        q = self.queues[p.name]
        st = self.stats[p.name]
        while True:
            alert: Alert = await q.get()
            try:
                await self._deliver(p, alert, st)
            finally:
                st.queue_depth = q.qsize()
                q.task_done()

    async def _deliver(self, p: Publisher, alert: Alert, st: PublisherStats) -> None:
        for attempt in range(self.retries + 1):
            try:
                await asyncio.wait_for(p.publish(alert), timeout=10)
                st.sent += 1
                if p.name not in alert.delivered_to:
                    alert.delivered_to.append(p.name)
                if self.on_delivered is not None:
                    try:
                        await self.on_delivered(alert)
                    except Exception:
                        log.exception("on_delivered callback failed")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                st.last_error = f"{type(exc).__name__}: {exc}"[:300]
                if attempt < self.retries:
                    st.retried += 1
                    await asyncio.sleep(self.base_delay * 2 ** attempt)
        st.failed += 1
        log.warning("%s failed for alert %s after %d attempts: %s",
                    p.name, alert.id, self.retries + 1, st.last_error)
        if self.fallback is not None and p is not self.fallback:
            try:
                await self.fallback.publish(alert, note=f"{p.name} failed: {st.last_error}")
            except Exception:
                log.exception("Fallback write failed for alert %s", alert.id)

    def status(self) -> dict:
        return {name: {**s.__dict__, "queue_depth": self.queues[name].qsize()}
                for name, s in self.stats.items()}
