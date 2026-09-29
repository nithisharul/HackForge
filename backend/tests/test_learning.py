"""Degraded mode, acknowledge/actions, and the learning runbook (proven fixes)."""
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.baseline import EWMABaseline
from app.core.detector import Detector
from app.core.severity import SeverityPolicy
from app.core.window import SlidingWindow
from app.explain.learning import fingerprint, merge_fingerprints, rank_fixes, similarity
from app.ml.calibration import Calibration
from app.ml.features import FeatureScaler
from app.ml.registry import ModelBundle


# ---------------------------------------------------------------- learning logic
DB = ["Connection refused to db-primary <IP>"]
OOM = ["OutOfMemoryError: Java heap space in worker-<NUM>"]


def _res(iid, templates, actions, success=True, steps=None, ttr=240, kind="error_spike", when="2026-09-29T10:00"):
    return {"incident_id": iid, "fingerprint": {"templates": templates, "kind": kind},
            "actions": actions, "steps": steps or len(actions), "ttr_seconds": ttr,
            "success": int(success), "created_at": when, "operator": "asha"}


def test_fingerprint_uses_unusual_errors_only():
    root = [{"template": "DB refused", "error_count": 30, "count": 30, "is_new": True, "lift": 9999},
            {"template": "OOM", "error_count": 10, "count": 10, "lift": 40},
            {"template": "auth failed", "error_count": 20, "count": 20, "lift": 1.2},   # background error
            {"template": "GET <PATH>", "error_count": 0, "count": 900, "is_new": True}]  # info noise
    fp = fingerprint(root, [], error_driven=True)
    assert fp == {"templates": ["DB refused", "OOM"], "kind": "error_spike"}
    # traffic drop: no errors at all -> fall back to new message types
    fp2 = fingerprint([{"template": "GET <PATH>", "error_count": 0, "count": 5, "is_new": True}],
                      [{"feature": "volume", "direction": "below"}], error_driven=False)
    assert fp2 == {"templates": ["GET <PATH>"], "kind": "traffic_drop"}


def test_fingerprint_grows_over_incident():
    early = {"templates": ["DB refused"], "kind": "error_spike"}
    later = {"templates": ["OOM", "DB refused"], "kind": "error_spike"}
    assert merge_fingerprints(early, later) == {"templates": ["DB refused", "OOM"], "kind": "error_spike"}
    assert merge_fingerprints(None, early) == early


def test_similarity():
    a = {"templates": DB, "kind": "error_spike"}
    assert similarity(a, a) == 1.0
    assert similarity(a, {"templates": OOM, "kind": "error_spike"}) == pytest.approx(0.3)
    # an early alert (subset of the error types) still fully matches a past incident
    assert similarity(a, {"templates": DB + OOM, "kind": "error_spike"}) == pytest.approx(1.0)
    assert similarity(a, {"templates": DB, "kind": "traffic_drop"}) == pytest.approx(0.7)


def test_rank_prefers_proven_then_fewer_steps():
    fp = {"templates": DB, "kind": "error_spike"}
    history = [
        _res("i1", DB, ["Restart pool", "Fail over DB", "Page DBA"], ttr=900),
        _res("i2", DB, ["Restart pool"], ttr=120),
        _res("i3", DB, ["restart  POOL"], ttr=180),            # same action, different wording
        _res("i4", DB, ["Clear cache"], success=False),         # never worked -> hidden
        _res("i5", OOM, ["Restart worker"]),                     # different incident type
    ]
    fixes = rank_fixes(fp, history)
    assert fixes[0]["actions"] == ["restart  POOL"] and fixes[0]["times_worked"] == 2
    assert fixes[0]["avg_minutes"] == 2.5
    assert fixes[1]["actions"] == ["Restart pool", "Fail over DB", "Page DBA"]
    assert all("Clear cache" not in f["actions"] and "Restart worker" not in f["actions"] for f in fixes)
    # a fix that fails as often as it works is no longer suggested
    history += [_res("i6", DB, ["Restart pool"], success=False), _res("i7", DB, ["Restart pool"], success=False)]
    assert [f["actions"] for f in rank_fixes(fp, history)] == [["Restart pool", "Fail over DB", "Page DBA"]]
    assert rank_fixes(fp, history, exclude_incident="i1") == []


# ---------------------------------------------------------------- degraded mode
class _Model:
    sequence_length = 1

    def __init__(self, fail=False):
        self.fail = fail

    def score(self, X):
        if self.fail:
            raise RuntimeError("weights corrupted")
        return np.array([0.5])

    def feature_contributions(self, X):
        return None


def _snap(err, n=1000):
    w = SlidingWindow(60, 20)
    for i in range(n):
        w.add(0, i < n * err, False, 1, False)
    return w.snapshot(0)


def test_failing_model_is_isolated_and_recovers():
    rng = np.random.default_rng(0)
    scaler = FeatureScaler.fit(rng.normal(size=(50, 6)) + 5)
    cal = Calibration(0.4, 0.6, 0.7, 100)
    bundle = ModelBundle(scaler=scaler, models={"good": _Model(), "bad": _Model(fail=True)},
                         calibrations={"good": cal, "bad": cal})
    d = Detector(EWMABaseline(warmup=2), SeverityPolicy(), bundle, ml_warmup_ticks=0)
    for _ in range(3):
        ev = d.evaluate(_snap(0.03))
    avail = {s.name: s.available for s in ev.detectors}
    assert avail == {"zscore": True, "good": True, "bad": False}
    assert ev.degraded == ["bad"]
    health = d.model_health()
    assert health["bad"]["status"] == "failed" and "weights corrupted" in health["bad"]["error"]
    assert health["good"]["status"] == "ok"
    # the system still detects a spike with the remaining detectors
    assert d.evaluate(_snap(0.5)).severity.name == "CRITICAL"
    # simulated failure (demo) + recovery via reload
    d.simulated_failures.add("good")
    assert set(d.evaluate(_snap(0.03)).degraded) == {"good", "bad"}
    bundle.models["bad"].fail = False
    d.set_bundle(bundle)
    ev = d.evaluate(_snap(0.03))
    assert ev.degraded == [] and not d.model_failures


# ---------------------------------------------------------------- end to end
@pytest.fixture()
def client(tmp_path, monkeypatch):
    for k, v in dict(log_file=tmp_path / "app.log", db_path=tmp_path / "alerts.db",
                     fallback_alert_file=tmp_path / "fb.jsonl", enable_ml=False,
                     baseline_warmup_ticks=5, tick_seconds=3600, aws_enabled=False,
                     incident_gap_seconds=0).items():
        monkeypatch.setattr(settings, k, v)
    from app.main import app
    with TestClient(app) as c:
        yield c


def _write(path, n, err):
    with open(path, "a") as fh:
        for i in range(n):
            lvl = "ERROR" if i < n * err else "INFO"
            fh.write(f"2026-09-29T10:00:00Z {lvl} [db] Connection refused to db-primary 10.0.5.{i % 9}:5432\n"
                     if lvl == "ERROR" else f"2026-09-29T10:00:00Z INFO [api] request {i} done\n")


def _spike(client, pipeline, total):
    _write(settings.log_file, 400, 0.6)
    total += 400
    end = time.time() + 3
    while pipeline.lines_processed < total and time.time() < end:
        time.sleep(0.05)
    client.portal.call(pipeline.evaluate_once, time.time())
    return total


def test_operator_workflow_teaches_the_system(client):
    p = client.app.state.pipeline
    total, now = 0, time.time()
    for k in range(8):                                   # learn a normal baseline
        _write(settings.log_file, 100, 0.03); total += 100
        end = time.time() + 3
        while p.lines_processed < total and time.time() < end:
            time.sleep(0.05)
        client.portal.call(p.evaluate_once, now + k)

    total = _spike(client, p, total)                     # incident #1
    alert = client.get("/alerts").json()[0]
    inc_id = alert["incident_id"]
    assert alert["proven_fixes"] == []                   # nothing learned yet

    detail = client.get(f"/incidents/{inc_id}").json()
    assert detail["fingerprint"]["kind"] == "error_spike"
    assert "Connection refused to db-primary <IP>" in detail["fingerprint"]["templates"]

    r = client.post(f"/incidents/{inc_id}/ack", json={"operator": "Nithish"})
    assert r.json()["ack_by"] == "Nithish"
    client.post(f"/incidents/{inc_id}/actions", json={
        "operator": "Nithish", "action": "Restarted the DB connection pool",
        "source": "recommended", "rec_id": "database_down:1"})
    client.post(f"/incidents/{inc_id}/actions", json={   # duplicate click is ignored
        "operator": "Nithish", "action": "Restarted the DB connection pool",
        "source": "recommended", "rec_id": "database_down:1"})
    client.post(f"/incidents/{inc_id}/actions", json={
        "operator": "Nithish", "action": "Failed over to db-replica", "source": "custom"})
    assert [a["action"] for a in client.get(f"/incidents/{inc_id}").json()["actions"]] == [
        "Restarted the DB connection pool", "Failed over to db-replica"]

    # can't confirm before it resolves
    assert client.post(f"/incidents/{inc_id}/resolution",
                       json={"operator": "Nithish", "fixed": True}).status_code == 409

    for k in range(3):                                   # 3 quiet windows -> RESOLVED
        client.portal.call(p.evaluate_once, time.time() + 100 + k)
    assert client.get(f"/incidents/{inc_id}").json()["status"] == "RESOLVED"

    r = client.post(f"/incidents/{inc_id}/resolution", json={"operator": "Nithish", "fixed": True}).json()
    assert r["learned"]["steps"] == 2 and "Learned" in r["message"]
    assert client.get("/incidents").json()[0]["confirmed"] == 1
    assert len(client.get("/learning/resolutions").json()) == 1
    # actions are closed once confirmed
    assert client.post(f"/incidents/{inc_id}/actions", json={
        "operator": "x", "action": "late action"}).status_code == 409

    _spike(client, p, total)                             # incident #2, same failure
    alert2 = client.get("/alerts").json()[0]
    assert alert2["incident_id"] != inc_id
    fix = alert2["proven_fixes"][0]
    assert fix["actions"] == ["Restarted the DB connection pool", "Failed over to db-replica"]
    assert fix["times_worked"] == 1 and fix["last_operator"] == "Nithish"


def test_break_model_route_and_health(client):
    r = client.post("/demo/break-model", json={"name": "lstm_ae"})
    assert r.status_code == 400                           # ML disabled in this test
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["pipeline"]["degraded"] == []
