"""Integration tests: tailer, dispatcher/publishers (AWS mocked with moto), ML
round-trip, and the pipeline end to end."""
import asyncio
import json
import os
import time

import boto3
import numpy as np
import pytest
from moto import mock_aws

from app.core.log_tailer import LogTailer
from app.ml.calibration import Calibration
from app.ml.features import FeatureScaler, featurize_lines, make_sequences
from app.ml.isolation_forest import IsolationForestModel
from app.models.schemas import Alert
from app.publishers.base import Publisher
from app.publishers.cloudwatch import CloudWatchPublisher
from app.publishers.dispatcher import Dispatcher
from app.publishers.local_file import LocalFilePublisher
from app.publishers.sns import SNSPublisher
from app.sim.simulator import LogSimulator


def _alert(sev="HIGH", status="OPEN"):
    return Alert(id="ALR-test", severity=sev, status=status, title="HIGH: error rate 40%",
                 description="test", error_rate=0.4, final_score=3.0, baseline_mean=0.03)


# ---------------------------------------------------------------- tailer
def test_tailer_handles_partial_lines_rotation_and_truncation(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("old line\n")

    async def run():
        tailer = LogTailer(path, from_end=True, poll_interval=0.02)
        got: list[str] = []

        async def consume():
            async for batch in tailer.lines():
                got.extend(batch)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        with open(path, "a") as fh:
            fh.write("a\nb")          # "b" is incomplete
        await asyncio.sleep(0.1)
        assert got == ["a"]
        with open(path, "a") as fh:
            fh.write("\n")
        await asyncio.sleep(0.1)
        assert got == ["a", "b"]
        os.replace(path, tmp_path / "app.log.1")   # rotation
        path.write_text("c\n")
        await asyncio.sleep(0.2)
        path.write_text("")                         # truncation
        await asyncio.sleep(0.1)
        with open(path, "a") as fh:
            fh.write("d\n")
        await asyncio.sleep(0.2)
        tailer.stop()
        await asyncio.wait_for(task, 1)
        return got, tailer

    got, tailer = asyncio.run(run())
    assert got == ["a", "b", "c", "d"]
    assert tailer.rotations == 1


# ---------------------------------------------------------------- publishers
@mock_aws
def test_cloudwatch_and_sns_publishers():
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
    logs = boto3.client("logs", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    sqs = boto3.client("sqs", region_name="us-east-1")
    topic = sns.create_topic(Name="alerts")["TopicArn"]
    q = sqs.create_queue(QueueName="inbox")["QueueUrl"]
    q_arn = sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=q_arn)

    cw = CloudWatchPublisher("/lad/alerts", "alerts", "us-east-1", client=logs)
    sp = SNSPublisher(topic, "us-east-1", min_severity="HIGH", client=sns)

    async def run():
        await cw.start()
        await cw.publish(_alert())
        assert sp.wants(_alert("HIGH")) and not sp.wants(_alert("MEDIUM"))
        await sp.publish(_alert())

    asyncio.run(run())
    events = logs.get_log_events(logGroupName="/lad/alerts", logStreamName="alerts")["events"]
    assert json.loads(events[0]["message"])["severity"] == "HIGH"
    msgs = sqs.receive_message(QueueUrl=q)["Messages"]
    body = json.loads(msgs[0]["Body"])
    assert "[HIGH]" in body["Subject"] and "Error rate:  40.0%" in body["Message"]


class _Flaky(Publisher):
    name = "flaky"

    def __init__(self, fail_times):
        self.fail_times, self.calls = fail_times, 0

    async def publish(self, alert):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("AWS unreachable")


class _Fast(Publisher):
    name = "fast"

    def __init__(self):
        self.got = []

    async def publish(self, alert):
        self.got.append(time.perf_counter())


def test_dispatcher_retries_then_falls_back(tmp_path):
    fallback = LocalFilePublisher(tmp_path / "fallback.jsonl")

    async def run():
        ok, bad = _Flaky(fail_times=2), _Flaky(fail_times=99)
        bad.name = "down"
        fast = _Fast()
        d = Dispatcher([ok, bad, fast], fallback, retries=2, base_delay=0.01)
        await d.start()
        t0 = time.perf_counter()
        d.submit(_alert())
        await d.drain(5)
        await d.stop()
        return d, ok, fast, t0

    d, ok, fast, t0 = asyncio.run(run())
    assert d.stats["flaky"].sent == 1 and d.stats["flaky"].retried == 2
    assert d.stats["down"].failed == 1
    assert fast.got[0] - t0 < 0.05                     # not blocked by the failing one
    rec = json.loads((tmp_path / "fallback.jsonl").read_text().splitlines()[0])
    assert "down failed" in rec["_fallback_reason"]


# ---------------------------------------------------------------- ML
def test_featurize_and_isolation_forest_roundtrip(tmp_path):
    sim = LogSimulator(rate=20, seed=1)
    lines = [l for _, l in sim.generate(0, 1800)]
    snaps = featurize_lines(lines)
    X_raw = np.array([s.vector() for s in snaps[12:]])
    scaler = FeatureScaler.fit(X_raw)
    X = scaler.transform(X_raw)
    m = IsolationForestModel(n_estimators=50).fit(X)
    cal = Calibration.fit(m.score(X))
    m.save(tmp_path)
    m2 = IsolationForestModel.load(tmp_path)
    assert np.allclose(m.score(X[:5]), m2.score(X[:5]))
    weird = scaler.transform(np.array([[5000, 2500, 0.5, 0.1, 30, 10]]))
    assert cal.normalize(m.score(weird)[0]) > 1.0     # far outside normal
    assert make_sequences(X, 10).shape == (len(X) - 9, 10, 6)


def test_lstm_autoencoder_if_available(tmp_path):
    lstm = pytest.importorskip("torch")  # noqa: F841
    from app.ml.lstm_autoencoder import LSTMAutoencoder
    rng = np.random.default_rng(0)
    X = rng.normal(0, 1, (300, 6))
    S = make_sequences(X, 10)
    ae = LSTMAutoencoder(epochs=3).fit(S)
    ae.save(tmp_path)
    ae2 = LSTMAutoencoder.load(tmp_path)
    assert np.allclose(ae.score(S[:3]), ae2.score(S[:3]), atol=1e-5)
    contrib = ae.feature_contributions(S[:1])
    assert contrib.shape == (6,) and abs(contrib.sum() - 1) < 1e-5
