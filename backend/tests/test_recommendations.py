"""Recommended actions: the right advice for the right evidence."""
from app.explain.recommendations import recommend


def _tpl(text, count=100, errors=100, new=False, lift=10.0):
    return {"template": text, "count": count, "error_count": errors, "is_new": new, "lift": lift}


BASE = dict(status="OPEN", action="OPEN", error_rate=0.4, baseline_mean=0.03,
            top_features=[], root_cause=[], consecutive=1)


def ids(recs):
    return [r["id"] for r in recs]


def test_critical_timeout_names_the_dependency():
    recs = recommend(**{**BASE, "severity": "CRITICAL",
                        "root_cause": [_tpl("Timeout calling payment-gateway after <NUM>ms")]})
    assert ids(recs)[:2] == ["take_ownership", "dependency_timeout"]
    assert "payment-gateway" in recs[1]["title"]
    assert "100 times" in recs[1]["reason"]
    assert all(r["steps"] for r in recs)


def test_database_and_oom_playbooks():
    recs = recommend(**{**BASE, "severity": "HIGH", "root_cause": [
        _tpl("Connection refused to db-primary <IP>", new=True),
        _tpl("OutOfMemoryError: Java heap space in worker-<NUM>", new=True)]})
    assert "database_down" in ids(recs) and "out_of_memory" in ids(recs)
    assert "never been seen before" in recs[0]["reason"]


def test_traffic_drop_without_errors():
    recs = recommend(**{**BASE, "severity": "MEDIUM", "error_rate": 0.03,
                        "top_features": [{"feature": "volume", "value": 150, "usual": 1200,
                                          "direction": "below"}]})
    assert ids(recs) == ["traffic_drop"]
    assert "150" in recs[0]["reason"]


def test_unknown_error_spike_gets_generic_advice():
    recs = recommend(**{**BASE, "severity": "MEDIUM",
                        "root_cause": [_tpl("Something odd happened in module <NUM>")]})
    assert ids(recs) == ["generic_errors"]


def test_sequence_escalation_and_still_open():
    recs = recommend(**{**BASE, "severity": "HIGH", "action": "ESCALATED"})
    assert "escalate" in ids(recs)
    recs = recommend(**{**BASE, "severity": "MEDIUM", "action": "REMINDER",
                        "minutes_open": 12, "incident_alerts": 5})
    assert "still_open" in ids(recs) and "5 alerts" in recs[-1]["reason"]


def test_resolved_gives_wrap_up_only():
    recs = recommend(**{**BASE, "severity": "CRITICAL", "status": "RESOLVED", "action": "RESOLVED"})
    assert ids(recs) == ["wrap_up"]


def test_never_empty_and_capped():
    assert recommend(**{**BASE, "severity": "LOW", "error_rate": 0.0})
    many = recommend(**{**BASE, "severity": "CRITICAL", "action": "ESCALATED", "root_cause": [
        _tpl("Timeout calling x-service"), _tpl("Connection refused to db-primary"),
        _tpl("Disk quota exceeded on <PATH>"), _tpl("OutOfMemoryError heap space")]})
    assert len(many) == 4
    assert many[0]["id"] == "take_ownership" and many[-1]["id"] == "escalate"