"""SQLite persistence for alerts, incidents, analyst feedback and the
operator response log that powers the learning runbook.

sqlite3 is synchronous, so every call runs in a worker thread via
asyncio.to_thread and the event loop never blocks on disk I/O.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.explain.learning import merge_fingerprints, rank_fixes, seconds_between
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
-- operator response: who owns an incident, what kind of incident it is,
-- and whether the operator confirmed their actions fixed it
CREATE TABLE IF NOT EXISTS incident_meta (
    incident_id TEXT PRIMARY KEY,
    fingerprint TEXT,
    ack_by TEXT,
    ack_at TEXT,
    confirmed INTEGER,            -- NULL = not asked yet, 1 = fixed it, 0 = did not
    confirmed_by TEXT,
    confirmed_at TEXT
);
-- every action an operator records while an incident is open
CREATE TABLE IF NOT EXISTS incident_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    operator TEXT,
    action TEXT NOT NULL,
    source TEXT,                  -- recommended | proven | custom
    rec_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_incident ON incident_actions(incident_id, id);
-- the learning table: one row per confirmed incident
CREATE TABLE IF NOT EXISTS resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    operator TEXT,
    fingerprint TEXT NOT NULL,
    actions TEXT NOT NULL,        -- JSON list, in the order they were taken
    steps INTEGER NOT NULL,
    ttr_seconds REAL NOT NULL,    -- time to resolve (incident start -> resolved)
    success INTEGER NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            self._exec,
            "SELECT i.payload, m.ack_by, m.ack_at, m.confirmed, "
            "(SELECT COUNT(*) FROM incident_actions a WHERE a.incident_id = i.id) AS n_actions "
            "FROM incidents i LEFT JOIN incident_meta m ON m.incident_id = i.id "
            "ORDER BY i.started_at DESC LIMIT ?", (limit,), True)
        out = []
        for r in rows:
            d = json.loads(r["payload"])
            d.update(ack_by=r["ack_by"], ack_at=r["ack_at"], confirmed=r["confirmed"],
                     action_count=r["n_actions"])
            out.append(d)
        return out

    async def get_incident(self, incident_id: str) -> dict | None:
        """Incident + owner + fingerprint + action log + its latest open alert."""
        def q():
            row = self._exec("SELECT payload FROM incidents WHERE id = ?", (incident_id,), True)
            if not row:
                return None
            d = json.loads(row[0]["payload"])
            meta = self._exec("SELECT * FROM incident_meta WHERE incident_id = ?", (incident_id,), True)
            m = dict(meta[0]) if meta else {}
            d.update(ack_by=m.get("ack_by"), ack_at=m.get("ack_at"), confirmed=m.get("confirmed"),
                     confirmed_by=m.get("confirmed_by"), confirmed_at=m.get("confirmed_at"),
                     fingerprint=json.loads(m["fingerprint"]) if m.get("fingerprint") else None)
            d["actions"] = [dict(a) for a in self._exec(
                "SELECT id, ts, operator, action, source, rec_id FROM incident_actions "
                "WHERE incident_id = ? ORDER BY id", (incident_id,), True)]
            latest = self._exec(
                "SELECT payload FROM alerts WHERE incident_id = ? AND status != 'RESOLVED' "
                "ORDER BY ts DESC LIMIT 1", (incident_id,), True)
            d["latest_alert"] = json.loads(latest[0]["payload"]) if latest else None
            res = self._exec("SELECT steps, ttr_seconds, success FROM resolutions WHERE incident_id = ?",
                             (incident_id,), True)
            d["resolution"] = dict(res[0]) if res else None
            return d
        return await asyncio.to_thread(q)

    # -- operator response & learning -------------------------------------------
    async def set_fingerprint(self, incident_id: str, fp: dict) -> dict:
        """Store / grow the incident's fingerprint (merged across its alerts)."""
        def q():
            row = self._exec("SELECT fingerprint FROM incident_meta WHERE incident_id = ?",
                             (incident_id,), True)
            old = json.loads(row[0]["fingerprint"]) if row and row[0]["fingerprint"] else None
            merged = merge_fingerprints(old, fp)
            self._exec(
                "INSERT INTO incident_meta (incident_id, fingerprint) VALUES (?, ?) "
                "ON CONFLICT(incident_id) DO UPDATE SET fingerprint = excluded.fingerprint",
                (incident_id, json.dumps(merged)))
            return merged
        return await asyncio.to_thread(q)

    async def acknowledge(self, incident_id: str, operator: str) -> dict:
        """'I'm on it'. Calling it again with another name reassigns the incident."""
        ts = _now()
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO incident_meta (incident_id, ack_by, ack_at) VALUES (?, ?, ?) "
            "ON CONFLICT(incident_id) DO UPDATE SET ack_by = excluded.ack_by, ack_at = excluded.ack_at",
            (incident_id, operator, ts))
        return {"ack_by": operator, "ack_at": ts}

    async def add_action(self, incident_id: str, operator: str | None, action: str,
                         source: str = "custom", rec_id: str | None = None) -> dict | None:
        """Record one action. Clicking the same recommended step twice is ignored."""
        def q():
            if rec_id and self._exec(
                    "SELECT 1 FROM incident_actions WHERE incident_id = ? AND rec_id = ?",
                    (incident_id, rec_id), True):
                return None
            ts = _now()
            self._exec(
                "INSERT INTO incident_actions (incident_id, ts, operator, action, source, rec_id) "
                "VALUES (?, ?, ?, ?, ?, ?)", (incident_id, ts, operator, action, source, rec_id))
            return {"ts": ts, "operator": operator, "action": action, "source": source, "rec_id": rec_id}
        return await asyncio.to_thread(q)

    async def confirm_resolution(self, incident_id: str, operator: str | None, fixed: bool) -> dict:
        """'Did your actions fix it?' Saves the learning record. Raises LookupError /
        ValueError if the incident doesn't exist or isn't resolved yet."""
        inc = await self.get_incident(incident_id)
        if inc is None:
            raise LookupError("incident not found")
        if inc["status"] != "RESOLVED":
            raise ValueError("incident is not resolved yet")
        actions = [a["action"] for a in inc["actions"]]
        ts = _now()

        def q():
            self._exec(
                "INSERT INTO incident_meta (incident_id, confirmed, confirmed_by, confirmed_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(incident_id) DO UPDATE SET "
                "confirmed = excluded.confirmed, confirmed_by = excluded.confirmed_by, "
                "confirmed_at = excluded.confirmed_at",
                (incident_id, int(fixed), operator, ts))
            if not actions or not inc.get("fingerprint"):
                self._exec("DELETE FROM resolutions WHERE incident_id = ?", (incident_id,))
                return None
            ttr = seconds_between(inc["started_at"], inc.get("resolved_at"))
            self._exec(
                "INSERT OR REPLACE INTO resolutions "
                "(incident_id, created_at, operator, fingerprint, actions, steps, ttr_seconds, success) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (incident_id, ts, operator or inc.get("ack_by"), json.dumps(inc["fingerprint"]),
                 json.dumps(actions), len(actions), ttr, int(fixed)))
            return {"steps": len(actions), "ttr_seconds": round(ttr, 1)}

        learned = await asyncio.to_thread(q)
        if learned is None:
            msg = "Recorded. No actions were logged, so there is nothing to learn from this incident."
        elif fixed:
            msg = (f"Learned: {learned['steps']} step(s) fixed this in "
                   f"{learned['ttr_seconds'] / 60:.1f} min. It will be suggested for similar incidents.")
        else:
            msg = "Recorded as not effective. These actions will rank lower for similar incidents."
        return {"fixed": fixed, "learned": learned, "message": msg}

    async def all_resolutions(self) -> list[dict]:
        rows = await asyncio.to_thread(self._exec, "SELECT * FROM resolutions ORDER BY id", (), True)
        out = []
        for r in rows:
            d = dict(r)
            d["fingerprint"] = json.loads(d["fingerprint"])
            d["actions"] = json.loads(d["actions"])
            out.append(d)
        return out

    async def find_proven_fixes(self, fp: dict, exclude_incident: str | None = None) -> list[dict]:
        return rank_fixes(fp, await self.all_resolutions(), exclude_incident)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
