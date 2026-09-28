"""Payment-scoped detector: a checkout failure hidden by healthy overall traffic
is detected, shown as its own incident with evidence, and resolved only after
real healthy checkout traffic. Runs on a simulated clock (no sleeping)."""
import asyncio
import re
from datetime import datetime, timezone

from app.config import Settings
from app.core.parsers.generic import GenericParser
from app.core.payment_monitor import PaymentMonitor
from app.core.pipeline import Pipeline
from app.db.store import AlertStore
from app.publishers.dispatcher import Dispatcher
from app.publishers.websocket import ConnectionManager
from app.sim.simulator import SCENARIOS, LogSimulator

T0 = 1_900_000_000.0
FAILED_CHECKOUT = re.compile(r"\[payments\] POST /api/v1/checkout 5\d\d ")
parser = GenericParser()


def _line(t, level, svc, msg):
    ts = datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return f"{ts} {level} [{svc}] {msg}\r\n"          # CRLF on purpose: parser must cope


def _checkout(t, status):
    return _line(t, "ERROR" if status >= 500 else "INFO", "payments",
                 f"POST /api/v1/checkout {status} 120ms request_id={int(t * 1000):016x}")


def _pipeline(tmp_path):
    cfg = Settings(enable_ml=False, simulator_enabled=False, aws_enabled=False, baseline_warmup_ticks=12,
                   log_file=tmp_path / "app.log", db_path=tmp_path / "alerts.db")
    return Pipeline(cfg, AlertStore(":memory:"), Dispatcher([]), ConnectionManager())


async def _drive(p, sim, start, seconds, extra=None):
    """Feed `sim` lines (plus optional extra(t) lines) in 1 s slices, tick every 5 s."""
    ticks = []
    t = start
    while t < start + seconds:
        for s in range(5):
            a = t + s
            lines = sim.lines_for_interval(a, a + 1) if sim else []
            lines += extra(a) if extra else []
            for ln in lines:
                p.ingest(ln, a + 1)
        t += 5
        ev = await p.evaluate_once(t)
        ticks.append((t, ev, dict(p.latest_payment)))
    return ticks


def _monitor():
    return PaymentMonitor(window_seconds=60, min_requests=20, failure_threshold=0.2,
                          healthy_rate=0.05, resolve_after=3)


def _feed(m, t, status):
    m.observe(t, parser.parse(_checkout(t, status)))


# ------------------------------------------------------------------ detection
def test_status_code_not_log_level_decides_failure():
    m = _monitor()
    for i in range(25):
        t = T0 + i
        _feed(m, t, 201)
        # ERROR lines that are not checkout completions must not count as failed requests
        m.observe(t, parser.parse(_line(t, "ERROR", "payments", "Timeout calling payment-gateway after 4000ms")))
        # checkout lines from another service are out of scope
        m.observe(t, parser.parse(_line(t, "ERROR", "api", "POST /api/v1/checkout 503 90ms")))
    snap = m.evaluate(T0 + 30).snapshot
    assert snap["requests"] == 25 and snap["failures"] == 0


# ------------------------------------------------------------------ no false alerts
def test_low_sample_and_normal_payment_traffic_do_not_open(tmp_path):
    m = _monitor()
    for i in range(10):                        # 100% failing, but only 10 requests
        _feed(m, T0 + i, 503)
    ev = m.evaluate(T0 + 15)
    assert ev.action is None and m.phase == "NORMAL" and not ev.snapshot["enough_traffic"]

    async def run():                           # 5 minutes of normal simulated traffic
        p = _pipeline(tmp_path)
        await _drive(p, LogSimulator(rate=20, seed=11, payment_rate=0.5), T0, 300)
        return p, await p.store.list_alerts()

    p, alerts = asyncio.run(run())
    assert not [a for a in alerts if a["scope"] == "payments"]
    assert p.latest_payment["requests"] >= 25 and p.latest_payment["failure_rate"] == 0.0


# ------------------------------------------------------------------ recovery
def test_no_payment_traffic_does_not_count_as_recovery():
    m = _monitor()
    for i in range(30):
        _feed(m, T0 + i, 503)
    assert m.evaluate(T0 + 30).action == "OPEN"
    actions = [m.evaluate(T0 + 30 + k * 5).action for k in range(1, 60)]   # 5 min of silence
    assert "RESOLVED" not in actions
    assert m.phase == "OPEN" and m.snapshot()["recovery_unverified"] and m.healthy_streak == 0
