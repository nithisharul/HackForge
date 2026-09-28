"""Pushes alerts to AWS CloudWatch Logs as structured JSON log events.

Each alert becomes one log event in `<log_group>/<log_stream>`, so you can use
CloudWatch Logs Insights, e.g.:

    fields @timestamp, severity, title
    | filter severity in ["HIGH", "CRITICAL"]
    | sort @timestamp desc

and attach a metric filter + CloudWatch Alarm on `{ $.severity = "CRITICAL" }`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import boto3
from botocore.config import Config

from app.models.schemas import Alert, Severity
from app.publishers.base import Publisher

log = logging.getLogger(__name__)


class CloudWatchPublisher(Publisher):
    name = "cloudwatch"
    min_severity = Severity.LOW

    def __init__(self, log_group: str, log_stream: str, region: str,
                 endpoint_url: str | None = None, client=None):
        self.log_group = log_group
        self.log_stream = log_stream
        self.client = client or boto3.client(
            "logs", region_name=region, endpoint_url=endpoint_url,
            config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
        )
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        for fn, kwargs in (
            (self.client.create_log_group, {"logGroupName": self.log_group}),
            (self.client.create_log_stream, {"logGroupName": self.log_group, "logStreamName": self.log_stream}),
        ):
            try:
                fn(**kwargs)
            except self.client.exceptions.ResourceAlreadyExistsException:
                pass
        self._ready = True

    async def start(self) -> None:
        try:
            await asyncio.to_thread(self._ensure)
            log.info("CloudWatch ready: %s/%s", self.log_group, self.log_stream)
        except Exception as exc:  # will retry lazily on first publish
            log.warning("CloudWatch setup failed (%s); will retry on publish", exc)

    def _put(self, alert: Alert) -> None:
        self._ensure()
        self.client.put_log_events(
            logGroupName=self.log_group,
            logStreamName=self.log_stream,
            logEvents=[{
                "timestamp": int(alert.ts.timestamp() * 1000) or int(time.time() * 1000),
                "message": json.dumps(alert.model_dump(mode="json")),
            }],
        )

    async def publish(self, alert: Alert) -> None:
        await asyncio.to_thread(self._put, alert)
