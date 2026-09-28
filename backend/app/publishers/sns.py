"""Publishes alerts to an AWS SNS topic (email / SMS / Slack via Chatbot / Lambda).

Only alerts at or above SNS_MIN_SEVERITY (default HIGH) are sent, because SNS
usually pages a human. A `severity` message attribute lets subscribers filter
further with an SNS subscription filter policy.
"""
from __future__ import annotations

import asyncio
import logging

import boto3
from botocore.config import Config

from app.models.schemas import Alert, Severity
from app.publishers.base import Publisher

log = logging.getLogger(__name__)


def format_message(alert: Alert) -> str:
    lines = [
        alert.title,
        "",
        alert.description,
        "",
        f"Status:      {alert.status}",
        f"Severity:    {alert.severity}",
        f"Error rate:  {alert.error_rate:.1%}"
        + (f" (baseline {alert.baseline_mean:.1%})" if alert.baseline_mean is not None else ""),
        f"Score:       {alert.final_score:.2f}  (1.0 = edge of normal)",
        f"Incident:    {alert.incident_id}",
        f"Time (UTC):  {alert.ts.isoformat()}",
    ]
    if alert.recommended_actions:
        lines += ["", "WHAT TO DO:"]
        for i, r in enumerate(alert.recommended_actions, 1):
            lines.append(f"{i}. {r['title']}")
            lines += [f"   - {step}" for step in r["steps"]]
    if alert.root_cause:
        lines += ["", "Suspicious log templates:"]
        lines += [f"  - [{r['count']}x] {r['template']}" for r in alert.root_cause[:3]]
    if alert.sample_lines:
        lines += ["", "Sample lines:"] + [f"  {s}" for s in alert.sample_lines[:3]]
    return "\n".join(lines)


class SNSPublisher(Publisher):
    name = "sns"

    def __init__(self, topic_arn: str, region: str, endpoint_url: str | None = None,
                 min_severity: str = "HIGH", client=None):
        self.topic_arn = topic_arn
        self.min_severity = Severity.parse(min_severity)
        self.client = client or boto3.client(
            "sns", region_name=region, endpoint_url=endpoint_url,
            config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
        )

    def _publish(self, alert: Alert) -> None:
        self.client.publish(
            TopicArn=self.topic_arn,
            Subject=f"[{alert.severity}] {alert.title}"[:100],
            Message=format_message(alert),
            MessageAttributes={
                "severity": {"DataType": "String", "StringValue": alert.severity},
                "status": {"DataType": "String", "StringValue": alert.status},
            },
        )

    async def publish(self, alert: Alert) -> None:
        await asyncio.to_thread(self._publish, alert)