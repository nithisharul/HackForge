"""Unit tests for the decision core: window, baseline, severity, alert manager,
parsers, template miner, incident grouping and the detector."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.core.alert_manager import AlertManager
from app.core.baseline import EWMABaseline
from app.core.detector import Detector
from app.core.incident_grouper import IncidentGrouper
from app.core.parsers import get_parser
from app.core.severity import SeverityPolicy
from app.core.template_miner import TemplateMiner, to_template
from app.core.window import SlidingWindow
from app.models.schemas import Severity


# ---------------------------------------------------------------- window
def test_window_rates_and_eviction():
    w = SlidingWindow(window_seconds=60, min_events=5)
    for i in range(10):
        w.add(ts=100 + i, is_error=i < 3, is_warn=i == 9, template_id=i % 2, is_new_template=False)
    s = w.snapshot(now=110)
    assert s.volume == 10 and s.error_count == 3
    assert s.error_rate == pytest.approx(0.3)
    assert s.warn_rate == pytest.approx(0.1)
    assert s.unique_templates == 2 and not s.low_confidence
    # 60s later the first 3 events (the errors) have left the window
    s2 = w.snapshot(now=163)
    assert s2.volume == 7 and s2.error_count == 0


def test_window_low_confidence_when_quiet():
    w = SlidingWindow(60, min_events=20)
    w.add(0, True, False, 1, True)
    assert w.snapshot(1).low_confidence


# ---------------------------------------------------------------- baseline
def test_baseline_warmup_and_zscore():
    b = EWMABaseline(alpha=0.1, warmup=5, min_std=0.01)
    for x in [0.02, 0.03, 0.025, 0.035, 0.03]:
        assert b.zscore(0.5) is None or b.ready
        b.update(x)
    assert b.ready
    assert b.mean == pytest.approx(0.028, abs=0.002)
    assert b.zscore(0.5) > 10
    assert abs(b.zscore(b.mean)) < 1e-9


def test_baseline_std_floor_prevents_explosion():
    b = EWMABaseline(warmup=3, min_std=0.01)
    for _ in range(5):
        b.update(0.0)
    assert b.std == 0.01
    assert b.zscore(0.02) == pytest.approx(2.0)


# ---------------------------------------------------------------- severity
def test_severity_bands():
    p = SeverityPolicy()
    assert p.from_score(0.5) == Severity.NONE
    assert p.from_score(1.2) == Severity.LOW
    assert p.from_score(2.0) == Severity.MEDIUM
    assert p.from_score(3.0) == Severity.HIGH
    assert p.from_score(9.0) == Severity.CRITICAL


def test_severity_overrides():
    p = SeverityPolicy(critical_error_rate=0.5, escalate_after=3)
    sev, reasons = p.classify(0.2, error_rate=0.6, consecutive=1)
    assert sev == Severity.CRITICAL and any("absolute" in r for r in reasons)
    sev, _ = p.classify(1.2, error_rate=0.1, consecutive=3)
    assert sev == Severity.MEDIUM  # LOW escalated one level
    sev, _ = p.classify(0.2, error_rate=0.6, consecutive=1, low_confidence=True)
    assert sev == Severity.NONE    # tiny windows can't trigger the absolute override


# ---------------------------------------------------------------- alert manager
def test_alert_manager_lifecycle():
    m = AlertManager(cooldown_seconds=60, resolve_after=2)
    assert m.process(Severity.NONE, 0) is None
    d = m.process(Severity.MEDIUM, 5, 1); assert d.action == "OPEN"
    assert m.process(Severity.MEDIUM, 10, 2) is None          # deduplicated
    assert m.process(Severity.LOW, 15, 3) is None             # lower: still deduped
    d = m.process(Severity.CRITICAL, 20, 4); assert d.action == "ESCALATED"
    assert m.process(Severity.CRITICAL, 25, 5) is None
    d = m.process(Severity.CRITICAL, 85, 6); assert d.action == "REMINDER"  # cooldown expired
    assert m.process(Severity.NONE, 90) is None               # 1 normal window
    d = m.process(Severity.NONE, 95); assert d.action == "RESOLVED" and d.peak == Severity.CRITICAL
    assert not m.active and m.suppressed == 3


def test_alert_manager_blip_does_not_resolve():
    m = AlertManager(resolve_after=3)
    m.process(Severity.HIGH, 0)
    m.process(Severity.NONE, 5)
    assert m.process(Severity.HIGH, 10) is None   # same incident, no new alert
    assert m.active


# ---------------------------------------------------------------- incidents
def test_incident_grouping_merges_flapping():
    g = IncidentGrouper(gap_seconds=120)
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = g.assign("OPEN", Severity.MEDIUM, t, "x")
    g.assign("UPDATE", Severity.HIGH, t + timedelta(seconds=30), "y")
    g.assign("RESOLVED", Severity.HIGH, t + timedelta(seconds=60), "y")
    b = g.assign("OPEN", Severity.LOW, t + timedelta(seconds=100), "z")      # within gap
    assert b.id == a.id and b.status == "OPEN" and b.peak_severity == "HIGH"
    g.assign("RESOLVED", Severity.LOW, t + timedelta(seconds=150), "z")
    c = g.assign("OPEN", Severity.LOW, t + timedelta(seconds=900), "w")      # new incident
    assert c.id != a.id


# ---------------------------------------------------------------- parsing
def test_generic_parser_formats():
    p = get_parser("generic")
    e = p.parse("2026-09-28T13:34:00.123Z ERROR [payments] Connection to 10.0.0.5 failed")
    assert e.level == "ERROR" and e.source == "payments" and e.is_error
    e = p.parse("2026-09-28 13:34:00,123 WARNING Slow query took 812ms")
    assert e.level == "WARN" and e.is_warn
    e = p.parse("    at com.example.Foo(Foo.java:42)")
    assert e.level == "INFO"          # continuation lines never counted as errors
    assert p.parse("   ") is None


def test_bgl_parser():
    line = ("KERNDTLB 1117869872 2005.06.04 R23-M0-NE-C:J15-U11 2005-06-04-00.24.32.432192 "
            "R23-M0-NE-C:J15-U11 RAS KERNEL FATAL data TLB error interrupt")
    e = get_parser("bgl").parse(line)
    assert e.level == "FATAL" and e.source == "label=KERNDTLB" and e.is_error


def test_template_miner_masks_variables():
    assert to_template("Connection to 10.0.0.5:5432 failed after 3012ms") == "Connection to <IP> failed after <NUM>ms"
    m = TemplateMiner()
    a, _, new_a = m.add("User 12 logged in")
    b, _, new_b = m.add("User 99 logged in")
    assert a == b and new_a and not new_b and m.frequency(a) == 1.0


# ---------------------------------------------------------------- detector
def _snap(err_rate, volume=1000, ts=0.0):
    w = SlidingWindow(60, 20)
    n_err = int(volume * err_rate)
    for i in range(volume):
        w.add(ts, i < n_err, False, 1, False)
    return w.snapshot(ts)


def test_detector_flags_spike_and_freezes_baseline():
    d = Detector(EWMABaseline(warmup=10), SeverityPolicy())
    rng = np.random.default_rng(0)
    for _ in range(30):
        ev = d.evaluate(_snap(0.03 + rng.normal(0, 0.005)))
        assert ev.severity == Severity.NONE
    mean_before = d.baseline.mean
    ev = d.evaluate(_snap(0.40))
    assert ev.severity == Severity.CRITICAL        # z huge
    assert d.baseline.mean == mean_before          # spike not learned
    ev = d.evaluate(_snap(0.07))
    assert ev.severity >= Severity.LOW


def test_detector_silent_during_warmup():
    d = Detector(EWMABaseline(warmup=10), SeverityPolicy())
    ev = d.evaluate(_snap(0.9))
    assert ev.severity == Severity.NONE and "warming" in ev.reasons[0]
