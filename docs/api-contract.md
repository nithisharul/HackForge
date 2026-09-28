# API contract

Base URL: `http://localhost:8000`. All timestamps are ISO-8601 UTC. Severity is one of
`NONE | LOW | MEDIUM | HIGH | CRITICAL`. Scores are normalised: **1.0 = edge of normal**.

## WebSocket `/ws/alerts`

On connect: `{"type": "hello", "data": [Alert, ...]}` (latest 20).
Then:
```json
{"type": "alert", "data": Alert}
{"type": "incident", "data": Incident}
```
Clients may send `"ping"`; the server answers `{"type": "pong"}`.

## WebSocket `/ws/metrics`

On connect: `{"type": "hello", "data": [Metrics, ...]}` (last 10 min). Then every tick:
`{"type": "metrics", "data": Metrics}`.

## Schemas

### Alert
```json
{
  "id": "ALR-3f9c0a1b2c",
  "incident_id": "INC-20260928-082608-a1b2",
  "ts": "2026-09-28T08:26:08.763Z",
  "status": "OPEN",                  // OPEN | ESCALATED | RESOLVED
  "severity": "CRITICAL",            // for RESOLVED: the incident's peak severity
  "title": "CRITICAL: error rate 38.2% (baseline 2.9%)",
  "description": "New anomaly: anomaly score 6.10 (1.0 = edge of normal)",
  "error_rate": 0.382,
  "baseline_mean": 0.029,
  "zscore": 18.3,
  "final_score": 6.1,
  "consecutive_windows": 1,
  "detectors": [
    {"name": "zscore",  "raw": 18.3, "normalized": 6.1,  "available": true},
    {"name": "iforest", "raw": 0.71, "normalized": 1.4,  "available": true},
    {"name": "lstm_ae", "raw": 0.93, "normalized": 10.9, "available": true}
  ],
  "top_features": [
    {"feature": "error_rate", "label": "error rate", "value": 0.382, "usual": 0.03,
     "direction": "above", "share": 0.71}
  ],
  "root_cause": [
    {"template_id": 17, "template": "Timeout calling payment-gateway after <NUM>ms",
     "count": 142, "error_count": 142, "share_now": 0.11, "share_usual": 0.01,
     "lift": 11.2, "is_new": false}
  ],
  "sample_lines": ["2026-09-28T08:26:08.101Z ERROR [api] Timeout calling payment-gateway after 4211ms"],
  "delivered_to": ["websocket", "cloudwatch", "sns"],
  "feedback": null                   // REST only: "TP" | "FP" | null
}
```

### Metrics
```json
{
  "ts": "...", "volume": 1204, "error_rate": 0.031, "warn_rate": 0.069,
  "baseline_mean": 0.029, "baseline_std": 0.01, "baseline_ready": true,
  "zscore": 0.2, "final_score": 0.31, "severity": "NONE", "low_confidence": false,
  "detectors": [ {"name": "zscore", "raw": 0.2, "normalized": 0.07, "available": true}, ... ]
}
```

### Incident
```json
{"id": "INC-...", "started_at": "...", "last_seen": "...", "resolved_at": null,
 "peak_severity": "HIGH", "alert_count": 3, "status": "OPEN", "title": "..."}
```

## REST

| Endpoint | Notes |
|---|---|
| `GET /alerts?limit=100&min_severity=HIGH&status=OPEN&incident_id=...` | newest first |
| `GET /alerts/{id}` | 404 if unknown |
| `POST /alerts/{id}/feedback` body `{"label": "TP"}` | returns feedback summary |
| `GET /incidents?limit=50` | newest first |
| `GET /metrics` | latest tick (null before the first tick) |
| `GET /metrics/history?minutes=15` | up to 60 minutes |
| `GET /health` | pipeline status, publisher stats, thresholds |
| `GET /benchmark` | 404 until `scripts/run_benchmark.py` has run |
| `GET /models`, `POST /models/reload` | loaded models, hot reload after retraining |
| `GET /demo/scenarios` | scenario name → description |
| `POST /demo/inject-anomaly` body `{"kind": "error_spike", "duration_seconds": 120}` | built-in simulator, or hands off to `generate_logs.py` |
