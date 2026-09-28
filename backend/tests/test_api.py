"""End-to-end through the real FastAPI app: lines appended to a file become an
alert that is stored, served by GET /alerts and pushed over the WebSocket."""
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "log_file", tmp_path / "app.log")
    monkeypatch.setattr(settings, "db_path", tmp_path / "alerts.db")
    monkeypatch.setattr(settings, "fallback_alert_file", tmp_path / "fallback.jsonl")
    monkeypatch.setattr(settings, "enable_ml", False)
    monkeypatch.setattr(settings, "baseline_warmup_ticks", 5)
    monkeypatch.setattr(settings, "tick_seconds", 3600)     # we drive ticks manually
    monkeypatch.setattr(settings, "aws_enabled", False)
    from app.main import app
    with TestClient(app) as c:
        yield c


def _write(path, n, err_rate):
    with open(path, "a") as fh:
        for i in range(n):
            lvl = "ERROR" if i < n * err_rate else "INFO"
            fh.write(f"2026-09-28T10:00:00Z {lvl} [api] request {i} done\n")


def _wait_for_lines(pipeline, n, timeout=3):
    end = time.time() + timeout
    while pipeline.lines_processed < n and time.time() < end:
        time.sleep(0.05)


def test_alert_flows_to_api_and_websocket(client):
    pipeline = client.app.state.pipeline
    portal = client.portal
    log = settings.log_file
    now = time.time()
    total = 0
    with client.websocket_connect("/ws/alerts") as ws:
        assert ws.receive_json()["type"] == "hello"
        for k in range(8):                      # normal: ~3% errors
            _write(log, 100, 0.03); total += 100
            _wait_for_lines(pipeline, total)
            portal.call(pipeline.evaluate_once, now + k)
        assert pipeline.detector.baseline.ready
        _write(log, 400, 0.6); total += 400      # spike
        _wait_for_lines(pipeline, total)
        ev = portal.call(pipeline.evaluate_once, now + 9)
        assert ev.severity.name == "CRITICAL"
        msgs = [ws.receive_json() for _ in range(2)]
        alert = next(m["data"] for m in msgs if m["type"] == "alert")
        assert alert["severity"] == "CRITICAL" and alert["status"] == "OPEN"
        assert alert["root_cause"] and alert["sample_lines"]

    alerts = client.get("/alerts").json()
    assert alerts[0]["id"] == alert["id"]
    assert client.get("/alerts", params={"min_severity": "CRITICAL"}).json()
    assert client.get("/incidents").json()[0]["peak_severity"] == "CRITICAL"
    r = client.post(f"/alerts/{alert['id']}/feedback", json={"label": "TP"})
    assert r.json()["summary"] == {"TP": 1}
    m = client.get("/metrics").json()
    assert m["error_rate"] > 0.15 and m["severity"] == "CRITICAL"
    assert client.get("/health").json()["status"] == "ok"
    assert "error_spike" in client.get("/demo/scenarios").json()
