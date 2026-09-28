"""SQLite persistence for alerts, incidents and analyst feedback.

sqlite3 is synchronous, so every call runs in a worker thread via
asyncio.to_thread and the event loop never blocks on disk I/O.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

from app.models.schemas import Alert, Incident, Severity

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    severity TEXT NOT NULL,
    severity_rank INTEGER NOT NULL,
    status TEXT NOT NULL,
    incident_id TEXT,
    payload TEXT NOT NULL,
    feedback TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class AlertStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _exec(self, sql: str, params: tuple = (), fetch: bool = False):
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall() if fetch else None
            self._conn.commit()
            return rows

    # -- alerts ---------------------------------------------------------------
    async def save_alert(self, alert: Alert) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT OR REPLACE INTO alerts (id, ts, severity, severity_rank, status, incident_id, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (alert.id, alert.ts.isoformat(), alert.severity, int(Severity.parse(alert.severity)),
             alert.status, alert.incident_id, alert.model_dump_json()),
        )

    async def list_alerts(self, limit: int = 100, min_severity: str | None = None,
                          status: str | None = None, incident_id: str | None = None) -> list[dict]:
        sql = "SELECT payload, feedback FROM alerts WHERE 1=1"
        params: list = []
        if min_severity:
            sql += " AND severity_rank >= ?"
            params.append(int(Severity.parse(min_severity)))
        if status:
            sql += " AND status = ?"
            params.append(status.upper())
        if incident_id:
            sql += " AND incident_id = ?"
            params.append(incident_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = await asyncio.to_thread(self._exec, sql, tuple(params), True)
        out = []
        for r in rows:
            d = json.loads(r["payload"])
            d["feedback"] = r["feedback"]
            out.append(d)
        return out

    async def get_alert(self, alert_id: str) -> dict | None:
        rows = await asyncio.to_thread(
            self._exec, "SELECT payload, feedback FROM alerts WHERE id = ?", (alert_id,), True)
        if not rows:
            return None
        d = json.loads(rows[0]["payload"])
        d["feedback"] = rows[0]["feedback"]
        return d

    async def set_feedback(self, alert_id: str, label: str) -> bool:
        await asyncio.to_thread(self._exec, "UPDATE alerts SET feedback = ? WHERE id = ?", (label, alert_id))
        return await self.get_alert(alert_id) is not None

    async def feedback_summary(self) -> dict:
        rows = await asyncio.to_thread(
            self._exec, "SELECT feedback, COUNT(*) AS n FROM alerts WHERE feedback IS NOT NULL GROUP BY feedback",
            (), True)
        return {r["feedback"]: r["n"] for r in rows}

    # -- incidents --------------------------------------------------------------
    async def upsert_incident(self, inc: Incident) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT OR REPLACE INTO incidents (id, started_at, status, payload) VALUES (?, ?, ?, ?)",
            (inc.id, inc.started_at.isoformat(), inc.status, inc.model_dump_json()),
        )

    async def list_incidents(self, limit: int = 50) -> list[dict]:
        rows = await asyncio.to_thread(
            self._exec, "SELECT payload FROM incidents ORDER BY started_at DESC LIMIT ?", (limit,), True)
        return [json.loads(r["payload"]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
