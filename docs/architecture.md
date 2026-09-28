# Architecture

## Data flow

1. **Ingest** – `LogTailer` polls the file (every 250 ms), yields only complete lines, and
   reopens on rotation (inode change) or rewinds on truncation (size shrinks). The parser
   turns each line into a `LogEvent` (timestamp, level, source, message). Unparseable lines
   count as INFO so volume stays honest without inventing errors.
2. **Template mining** – `TemplateMiner` masks variable parts (IPs, numbers, UUIDs, paths…)
   so `Connection to 10.0.0.5 failed` → `Connection to <IP> failed`. Each template gets an ID
   and a running count. This yields two features (distinct templates, never-seen templates)
   and powers root-cause hints.
3. **Sliding window** – events enter a 60 s window keyed by *arrival* time. Every 5 s a
   snapshot evicts old events and emits a 6-feature vector:
   `volume, error_count, error_rate, warn_rate, unique_templates, new_templates`.
   Windows with < 20 events are flagged low-confidence (the error rate of 3 lines means nothing).
4. **Detect** (`core/detector.py`, pure and synchronous so live, offline and tests share it)
   * **Baseline z-score** on error rate: EWMA mean/variance (α = 0.05), 24-tick warm-up,
     σ floor 0.01, updated only on normal windows.
   * **Isolation Forest** on the scaled feature vector of one window.
   * **LSTM-Autoencoder** on the last 10 windows, scored on the reconstruction error of the
     latest 2 steps.
5. **Calibrate + ensemble** – each ML score becomes `(raw − median) / (p99 − median)` on held-out
   normal data; the z-score becomes `z / 3`. So **1.0 = edge of normal** for every detector.
   `final = max(z_norm, weighted_mean(all available))`: ML can raise the score but never hide
   an error spike. ML scores above 1.0 are log-compressed (`1 + ln x`) before averaging, so one
   model far out on a limb (typically the LSTM-AE still remembering a spike that just ended)
   cannot drive severity on its own.
6. **Severity** – `<1 NONE, ≥1 LOW, ≥1.5 MEDIUM, ≥2.5 HIGH, ≥4 CRITICAL`; error rate ≥ 50 % is
   always CRITICAL; ≥3 consecutive anomalous windows escalate one level. Nothing alerts during warm-up.
7. **Alert manager** – OPEN on the first anomalous window, ESCALATED immediately if severity
   rises, REMINDER after the 60 s cooldown, RESOLVED after 3 normal windows. Everything else
   is suppressed (counted in `/health`).
8. **Incidents** – each OPEN→RESOLVED episode is an incident. A new episode within 120 s of
   the last one resolving reopens the same incident (flapping).
9. **Explain** – every alert carries: detector scores, top 3 features (LSTM-AE per-feature
   reconstruction error, or distance from the training mean), top suspicious templates (new,
   error-heavy, or far above their usual share), and 5 sample error lines.
10. **Deliver** – alert saved to SQLite, then `Dispatcher.submit()` puts it on one queue per
    publisher (WebSocket, CloudWatch, SNS, local file). Workers retry with exponential backoff
    and fall back to `alerts_fallback.jsonl`. The tick loop never awaits AWS.

## Concurrency model

One asyncio event loop runs: tail task, tick task, optional simulator task, one worker per
publisher, and the FastAPI/WebSocket handlers. Blocking work (boto3, sqlite3) runs in
`asyncio.to_thread`. A tick takes about 2–5 ms including both ML models, so a single process
comfortably handles thousands of lines per second.

## Design decisions

| Decision | Why |
|---|---|
| Arrival time instead of log timestamp for the window | Robust to clock skew and to replaying old files; real-time is what matters for alerting |
| Baseline freezes during anomalies | Otherwise a long incident slowly becomes the "new normal" and stops alerting |
| σ floor | A perfectly quiet system has σ≈0, so one stray error would score z = 1000 |
| Percentile calibration | Raw scores from different models are not comparable; percentiles are, and they state the false-alarm rate |
| `max(z, mean)` ensemble | Keeps the statistical safety net intact when an ML model is wrong, missing or still warming up |
| Per-publisher queues | A 10-second AWS timeout must never freeze the dashboard |
| Pure `Detector` class | Offline benchmark numbers are produced by the exact code that runs live |

## Scaling beyond one file

* Many files/services: run one `Pipeline` per stream (key the baseline per service), or
  ship logs through Kinesis/Kafka and replace `LogTailer` with a consumer, since the rest of the
  pipeline takes lines and is unchanged.
* Multiple dashboard replicas: put alerts on Redis pub/sub (or SNS→SQS) and have each API
  replica broadcast to its own WebSocket clients.
* Seasonality (nightly batch jobs): keep one baseline per hour-of-week bucket.
