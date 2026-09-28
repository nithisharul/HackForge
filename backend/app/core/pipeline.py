"""Orchestrates the live system.

    tail loop  : file -> parser -> template miner -> sliding window   (continuous)
    tick loop  : every TICK_SECONDS
                   window snapshot -> Detector (baseline + ML + severity)
                   -> metrics broadcast
                   -> AlertManager (dedup / cooldown / escalate / resolve)
                   -> IncidentGrouper -> root cause -> SQLite -> Dispatcher
                   -> PaymentMonitor (checkout 5xx rate; runs even when the
                      global detector says NONE) -> own IncidentGrouper -> same store/dispatch

Events are placed in the window by ARRIVAL time (wall clock), not by the
timestamp written in the line. That keeps the system correct when producers
have skewed clocks or when an old file is replayed.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timezone

from app.config import Settings
from app.core.alert_manager import AlertDecision, AlertManager
from app.core.baseline import EWMABaseline
from app.core.detector import Detector, Evaluation
from app.core.incident_grouper import IncidentGrouper
from app.core.log_tailer import LogTailer
from app.core.payment_monitor import PaymentEvaluation, PaymentMonitor
from app.core.parsers import get_parser
from app.core.severity import SeverityPolicy
from app.core.template_miner import TemplateMiner
from app.core.window import SlidingWindow
from app.db.store import AlertStore
from app.explain.root_cause import rank_templates
from app.ml.registry import ModelBundle, load_bundle
from app.models.schemas import Alert, Metrics, Severity
from app.publishers.dispatcher import Dispatcher
from app.publishers.websocket import ConnectionManager
from app.sim.simulator import LogSimulator

log = logging.getLogger(__name__)

PAYMENT_TITLE = "Payment checkout failure spike"
EVIDENCE_LABEL = "Supporting evidence: matching checkout log lines (not a proven root cause)"


class Pipeline:
    def __init__(self, cfg: Settings, store: AlertStore, dispatcher: Dispatcher,
                 ws: ConnectionManager):
        self.cfg = cfg
        self.store = store
        self.dispatcher = dispatcher
        self.ws = ws
        self.parser = get_parser(cfg.log_format)
        self.miner = TemplateMiner()
        self.window = SlidingWindow(cfg.window_seconds, cfg.min_events_per_window)
        self.tailer = LogTailer(cfg.log_file, cfg.tail_from_end, cfg.tail_poll_interval)
        self.bundle: ModelBundle = load_bundle(cfg.artifacts_dir) if cfg.enable_ml else ModelBundle()
        self.detector = Detector(
            EWMABaseline(cfg.baseline_alpha, cfg.baseline_warmup_ticks, cfg.baseline_min_std),
            SeverityPolicy(cfg.sev_low, cfg.sev_medium, cfg.sev_high, cfg.sev_critical,
                           cfg.critical_error_rate, cfg.escalate_after_windows),
            self.bundle,
            {"zscore": cfg.weight_zscore, "iforest": cfg.weight_iforest, "lstm_ae": cfg.weight_lstm},
            cfg.sequence_length,
            ml_warmup_ticks=int(cfg.window_seconds / cfg.tick_seconds),
        )
        self.detector.set_bundle(self.bundle)
        self.alerts = AlertManager(cfg.alert_cooldown_seconds, cfg.resolve_after_normal_windows)
        self.incidents = IncidentGrouper(cfg.incident_gap_seconds)
        self.payments = PaymentMonitor(
            cfg.window_seconds, cfg.payment_source, cfg.payment_route, cfg.payment_failure_threshold,
            cfg.payment_healthy_rate, cfg.payment_min_requests, cfg.payment_resolve_after_windows,
            cfg.payment_evidence_lines)
        # separate grouper: IncidentGrouper tracks ONE current incident, and a
        # global anomaly must never be merged into the payment incident (or vice versa)
        self.payment_incidents = IncidentGrouper(cfg.incident_gap_seconds)
        self.latest_payment: dict | None = None
        self.simulator: LogSimulator | None = (
            LogSimulator(rate=cfg.simulator_rate, payment_rate=cfg.simulator_payment_rate)
            if cfg.simulator_enabled else None)

        self.metrics_history: deque[dict] = deque(maxlen=int(3600 / cfg.tick_seconds))
        self.latest: Evaluation | None = None
        self.lines_processed = 0
        self.parse_failures = 0
        self.started_at = time.time()
        self.tick_count = 0
        self.last_tick_ms = 0.0
        self._tasks: list[asyncio.Task] = []
        self._open_title = ""

    # ---- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        self.cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.log_file.touch(exist_ok=True)
        if self.simulator:
            self._tasks.append(asyncio.create_task(self._simulate(), name="simulator"))
        self._tasks.append(asyncio.create_task(self._tail(), name="tail"))
        self._tasks.append(asyncio.create_task(self._tick(), name="tick"))
        log.info("Pipeline started: file=%s window=%ss tick=%ss models=%s",
                 self.cfg.log_file, self.cfg.window_seconds, self.cfg.tick_seconds,
                 list(self.bundle.models) or "none (statistical only)")

    async def stop(self) -> None:
        self.tailer.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def reload_models(self) -> list[str]:
        self.bundle = load_bundle(self.cfg.artifacts_dir)
        self.detector.set_bundle(self.bundle)
        return list(self.bundle.models)

    # ---- loops ---------------------------------------------------------------
    async def _tail(self) -> None:
        async for batch in self.tailer.lines():
            now = time.time()
            for line in batch:
                self.ingest(line, now)

    def ingest(self, line: str, now: float) -> None:
        """One raw line -> global window + payment monitor (arrival time `now`)."""
        ev = self.parser.parse(line)
        if ev is None:
            self.parse_failures += 1
            return
        tid, _tpl, is_new = self.miner.add(ev.message)
        self.window.add(now, ev.is_error, ev.is_warn, tid, is_new, ev.raw)
        self.payments.observe(now, ev)
        self.lines_processed += 1

    async def _simulate(self) -> None:
        """Built-in log generator for the one-command demo."""
        step = 0.2
        last = time.time()
        while True:
            await asyncio.sleep(step)
            now = time.time()
            lines = self.simulator.lines_for_interval(last, now)
            last = now
            if lines:
                with open(self.cfg.log_file, "a") as fh:
                    fh.write("\n".join(lines) + "\n")

    async def _tick(self) -> None:
        next_at = time.time() + self.cfg.tick_seconds
        while True:
            await asyncio.sleep(max(0.0, next_at - time.time()))
            next_at += self.cfg.tick_seconds
            try:
                await self.evaluate_once(time.time())
            except Exception:
                log.exception("Tick failed; continuing")

    # ---- one evaluation ---------------------------------------------------------
    async def evaluate_once(self, now: float) -> Evaluation:
        t0 = time.perf_counter()
        snap = self.window.snapshot(now, self.miner.names)
        ev = self.detector.evaluate(snap)
        self.latest = ev
        self.tick_count += 1
        pay = self.payments.evaluate(now)      # every tick, whatever the global verdict
        self.latest_payment = pay.snapshot

        metrics = Metrics(
            ts=snap.ts, volume=snap.volume, error_rate=snap.error_rate, warn_rate=snap.warn_rate,
            baseline_mean=ev.baseline_mean, baseline_std=ev.baseline_std,
            baseline_ready=ev.baseline_ready,
            zscore=round(ev.zscore, 3) if ev.zscore is not None else None,
            final_score=ev.final_score, severity=ev.severity.name,
            low_confidence=snap.low_confidence, detectors=ev.detectors, payment=pay.snapshot,
        ).model_dump(mode="json")
        self.metrics_history.append(metrics)
        await self.ws.broadcast("metrics", {"type": "metrics", "data": metrics})

        decision = self.alerts.process(ev.severity, now, ev.consecutive)
        if decision:
            await self._raise_alert(ev, decision)
        if pay.action:
            await self._payment_incident(pay, ev)
        self.last_tick_ms = (time.perf_counter() - t0) * 1000
        return ev

    async def _raise_alert(self, ev: Evaluation, d: AlertDecision) -> None:
        snap = ev.snapshot
        now_dt = datetime.now(timezone.utc)
        root = rank_templates(self.window.template_counts(), self.window.error_template_counts(), self.miner)

        if d.action == "RESOLVED":
            severity = d.peak
            title = f"RESOLVED: {self._open_title or 'anomaly'}"
            status = "RESOLVED"
            description = (f"Behaviour back to normal for {self.cfg.resolve_after_normal_windows} "
                           f"consecutive windows. Error rate now {snap.error_rate:.1%}.")
        else:
            severity = d.severity
            title = self._title(ev, severity)
            if d.action == "REMINDER" and severity < self.alerts.peak_at_last_alert:
                sev_name, _, rest = title.partition(": ")
                title = f"{sev_name}: recovering - {rest}"   # still above normal, but past the peak
            status = "ESCALATED" if d.action == "ESCALATED" else "OPEN"
            prefix = {"OPEN": "New anomaly", "ESCALATED": "Anomaly escalated",
                      "REMINDER": "Anomaly still ongoing"}[d.action]
            description = f"{prefix}: " + "; ".join(ev.reasons)
            if d.action == "OPEN" or severity >= self.alerts.peak_at_last_alert:
                # the RESOLVED message names the worst thing that happened
                self._open_title = title.split(": ", 1)[-1]

        incident = self.incidents.assign(
            "RESOLVED" if d.action == "RESOLVED" else ("OPEN" if d.action == "OPEN" else "UPDATE"),
            severity, now_dt, title)
        alert = Alert(
            id=f"ALR-{uuid.uuid4().hex[:10]}",
            incident_id=incident.id,
            ts=now_dt,
            status=status,
            severity=severity.name,
            title=title,
            description=description,
            error_rate=snap.error_rate,
            baseline_mean=ev.baseline_mean,
            zscore=round(ev.zscore, 3) if ev.zscore is not None else None,
            final_score=ev.final_score,
            consecutive_windows=d.consecutive,
            detectors=ev.detectors,
            top_features=ev.top_features,
            root_cause=root if d.action != "RESOLVED" else [],
            sample_lines=self.window.recent_error_lines(5) if d.action != "RESOLVED" else [],
        )
        await self.store.save_alert(alert)
        await self.store.upsert_incident(incident)
        await self.ws.broadcast("alerts", {"type": "incident", "data": incident.model_dump(mode="json")})
        self.dispatcher.submit(alert)
        log.info("ALERT %s %s %s", alert.status, alert.severity, alert.title)

    async def _payment_incident(self, pay: PaymentEvaluation, ev: Evaluation) -> None:
        """OPEN / RESOLVED -> one alert each; UPDATE -> refresh the stored incident only."""
        s = pay.snapshot
        now_dt = datetime.now(timezone.utc)
        inc = self.payment_incidents.current
        if pay.action == "UPDATE":
            if inc is None or inc.status == "RESOLVED":
                return
            inc.last_seen = now_dt
            inc.details = {**inc.details, **s}
            await self.store.upsert_incident(inc)
            await self.ws.broadcast("alerts", {"type": "incident", "data": inc.model_dump(mode="json")})
            return

        rate = f"{s['failures']}/{s['requests']} checkout requests ({s['failure_rate']:.1%})"
        glob = f"overall error rate {ev.snapshot.error_rate:.1%}, global detector {ev.severity.name}"
        if pay.action == "OPEN":
            severity = Severity.HIGH if s["failure_rate"] >= 0.5 else Severity.MEDIUM
            title, status = PAYMENT_TITLE, "OPEN"
            description = (f"{rate} returned HTTP 5xx in the last {s['window_seconds']:.0f} s "
                           f"(threshold {s['threshold']:.0%}, min {s['min_requests']} requests); {glob}.")
        else:
            severity = Severity.parse(inc.peak_severity) if inc else Severity.MEDIUM
            title, status = f"RESOLVED: {PAYMENT_TITLE}", "RESOLVED"
            description = (f"Checkout recovered: {s['resolve_after']} consecutive healthy windows with real "
                           f"traffic; now {rate} failing (peak {s['peak_failure_rate']:.1%}).")
        inc = self.payment_incidents.assign(pay.action, severity, now_dt, PAYMENT_TITLE)
        inc.scope = "payments"
        inc.details = {**s, "evidence_label": EVIDENCE_LABEL,
                       "evidence": pay.evidence if pay.action == "OPEN" else inc.details.get("evidence", []),
                       **({"recovery_evidence": pay.evidence} if pay.action == "RESOLVED" else {})}
        alert = Alert(
            id=f"ALR-{uuid.uuid4().hex[:10]}", incident_id=inc.id, ts=now_dt, status=status,
            severity=severity.name, title=title, description=description,
            error_rate=ev.snapshot.error_rate,               # still the GLOBAL error rate
            final_score=round(s["failure_rate"] / s["threshold"], 3) if s["threshold"] else 0.0,
            sample_lines=pay.evidence, scope="payments",
        )
        await self.store.save_alert(alert)
        await self.store.upsert_incident(inc)
        await self.ws.broadcast("alerts", {"type": "incident", "data": inc.model_dump(mode="json")})
        self.dispatcher.submit(alert)
        log.info("PAYMENT ALERT %s %s %s", alert.status, alert.severity, description)

    def _title(self, ev: Evaluation, sev: Severity) -> str:
        s = ev.snapshot
        top = ev.top_features[0]["feature"] if ev.top_features else None
        err_driven = ((ev.zscore is not None and ev.zscore >= 3)
                      or s.error_rate >= self.cfg.critical_error_rate
                      or top in ("error_rate", "error_count"))
        if err_driven:
            base = f" (baseline {ev.baseline_mean:.1%})" if ev.baseline_mean is not None else ""
            return f"{sev.name}: error rate {s.error_rate:.1%}{base}"
        if ev.top_features:
            f = ev.top_features[0]
            return f"{sev.name}: unusual log pattern - {f['label']} {f.get('direction', 'off')} normal"
        return f"{sev.name}: unusual log pattern"

    # ---- status ------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "log_file": str(self.cfg.log_file),
            "lines_processed": self.lines_processed,
            "tailer": {"lines_read": self.tailer.lines_read, "rotations": self.tailer.rotations,
                       "truncations": self.tailer.truncations},
            "templates_known": len(self.miner),
            "window_events": len(self.window),
            "ticks": self.tick_count,
            "last_tick_ms": round(self.last_tick_ms, 2),
            "baseline": self.detector.baseline.to_dict(),
            "models_loaded": list(self.bundle.models),
            "model_metadata": {k: self.bundle.metadata.get(k) for k in ("trained_at", "windows_total")},
            "alert_manager": {"active": self.alerts.active, "suppressed": self.alerts.suppressed,
                              "peak": self.alerts.peak.name},
            "simulator": bool(self.simulator),
            "payment": self.latest_payment,
            "websocket_clients": self.ws.count(),
        }
