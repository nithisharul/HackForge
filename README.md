# Real-Time Log Anomaly Detector with Alert Feed

Watches a continuously growing log file and answers one question every 5 seconds:
**is the system behaving abnormally right now?** When it is, it raises an alert with a
severity level, shows it on a live dashboard over WebSockets, and pushes it to
**AWS CloudWatch Logs** and **SNS**.

```
log file ─► tailer ─► parser ─► template miner ─► sliding window (60 s)
                                                        │ feature vector every 5 s
             ┌──────────────────────────────┬───────────┴───────────┐
      EWMA baseline z-score          Isolation Forest          LSTM-Autoencoder
             └──────────── calibrated to one scale (1.0 = edge of normal) ─────┘
                                          │
                                  ensemble → severity (NONE…CRITICAL)
                                          │
                         alert manager (dedup · cooldown · escalate · resolve)
                                          │
                         incident grouper → root cause → SQLite
                                          │
                  dispatcher ─► WebSocket dashboard │ CloudWatch Logs │ SNS │ local file
```

## Recommended actions ("What to do")

Every alert carries 1-4 plain-language recommended actions so an inexperienced
operator knows where to start. They come from an editable playbook
(`backend/app/explain/recommendations.py`) matched against the evidence in the alert:

* **Severity / lifecycle**: CRITICAL means "take ownership, tell the team"; RESOLVED means "wrap up".
* **Log content**: timeouts, database refused, out of memory, disk full, auth failures,
  circuit breakers, malformed requests, each with concrete steps.
* **Anomaly shape**: traffic dropped or surged, brand-new messages, unexplained error spike.
* **Incident history**: escalating, or still unresolved after 5+ minutes, means escalate.

Each action includes the reason ("'Connection refused to db-primary' appeared 27 times in
the last minute and has never been seen before"). The actions appear on the dashboard,
in the SNS message and in CloudWatch Logs. They are first-response guidance, not
guaranteed fixes.

## Incident response and the learning runbook

The **Incident response** panel (top right of the dashboard) is where an operator works an incident:

1. **I'm on it**: takes ownership (or **Take over** to reassign). The owner shows in the Incidents table.
2. **Did this**: on any recommended step, or **Add** a custom action ("restarted payment-worker-3"). Every action is logged with who did it and when.
3. When the system detects recovery, it asks **"Did your actions fix it?"**
   * **Yes**: the ordered actions are saved as a **proven fix** for this kind of incident.
   * **No**: they are recorded as not effective and rank lower next time.
4. The next time a **similar incident** appears (the same unusual error types, the same kind of anomaly),
   the alert shows **"✔ Proven fix: worked N× before, avg X steps, Y min, last by …"** above the playbook.

Ranking: net successes (worked minus failed) first, then fewest steps, then fastest resolution. A fix that
fails as often as it works stops being suggested. Logic: `backend/app/explain/learning.py`; storage:
`incident_meta`, `incident_actions` and `resolutions` tables in `alerts.db`.

## Degraded mode (a detector fails)

Each ML model is isolated. If one crashes while scoring, it is marked **failed** and skipped, the ensemble
averages the detectors that are still working (all share one calibrated scale, so thresholds stay the same),
and the z-score keeps running regardless. `/health` reports `"status": "degraded"` with the error, and the
dashboard's Models pill turns amber. Recovery: retrain if needed, then `POST /models/reload`, with no restart.
Try it with the dashboard's **Break LSTM-AE** and **Reload models** buttons.

## Requirements coverage

| Requirement | Where |
|---|---|
| Monitor a continuously growing log file | `core/log_tailer.py` – async `tail -F`, survives rotation and truncation, buffers half-written lines |
| Rolling error rate with a sliding window | `core/window.py` – 60 s time window, evaluated every 5 s, O(1) running counters |
| Baseline for normal behaviour | `core/baseline.py` – EWMA mean/σ, 2-min warm-up, frozen during anomalies, σ floor |
| Detect deviations | `core/detector.py` – z-score + Isolation Forest + LSTM-AE, calibrated and blended (`ml/`) |
| Severity levels | `core/severity.py` – NONE / LOW / MEDIUM / HIGH / CRITICAL + absolute-rate and persistence overrides |
| Real-time frontend (WebSockets or polling) | `frontend/index.html` – WebSocket with automatic polling fallback |
| Display alerts as generated | Live alert feed, charts, incidents, benchmark table |
| Push alerts to CloudWatch Logs or SNS | `publishers/cloudwatch.py`, `publishers/sns.py` – both, with retries and local-file fallback |

## Quick start (local, ~3 minutes)

```bash
unzip log-anomaly-detector.zip && cd log-anomaly-detector
./scripts/demo.sh            # venv, deps, data, training, benchmark, server
```
Open **http://localhost:8000**. The baseline warms up for about 2 minutes (the status pill
says "warming up"), then click an **inject** button, e.g. `error spike`, and watch the
error-rate line leave the normal band, the alert appear in the feed, and a RESOLVED alert
arrive once it's over.

### Manual steps (what demo.sh does)

```bash
python -m venv .venv && source .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu   # optional (LSTM-AE)
pip install -r backend/requirements.txt

python scripts/generate_training_data.py             # SIMULATED 6 h normal + 2 h labelled test
cd backend && python -m app.ml.train --data ../data/train_normal.log && cd ..
python scripts/run_benchmark.py                      # model comparison table

# Terminal 1: the system being watched
python scripts/generate_logs.py --rate 20
# Terminal 2: the detector + dashboard
cd backend && uvicorn app.main:app --port 8000
```
Trained models are already included in `backend/artifacts/`, so training is only needed if
you change the data. Without trained models (or without PyTorch) the system still runs: the
ensemble uses whatever detectors are available.

### Watch your own log file
```bash
cd backend
LOG_FILE=/var/log/myapp/app.log uvicorn app.main:app
```
The generic parser accepts `<ISO timestamp> <LEVEL> [source] message` (see
`core/parsers/generic.py`). For other formats add a parser class. **Retrain on a few hours
of your own normal logs** (`python -m app.ml.train --data yourfile.log`): the shipped models
learned the simulator's traffic profile (≈20 lines/s), and would flag your volume as unusual.
The z-score detector needs no training and adapts to any log on its own.

## AWS

Set in `backend/.env` (see `.env.example`):
```
AWS_ENABLED=true
AWS_REGION=us-east-1
CLOUDWATCH_LOG_GROUP=/log-anomaly-detector/alerts
SNS_TOPIC_ARN=arn:aws:sns:us-east-1:123456789012:log-anomaly-alerts
SNS_MIN_SEVERITY=HIGH
```
* **CloudWatch Logs** receives every alert (LOW+) as a JSON log event; the group and stream are
  created automatically. Query with Logs Insights, or add a metric filter
  `{ $.severity = "CRITICAL" }` plus a CloudWatch Alarm.
* **SNS** receives HIGH+ alerts (configurable) with a readable body and a `severity`
  message attribute for subscription filter policies (email, SMS, Slack via Chatbot, Lambda).
* Minimal IAM policy: `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`,
  `sns:Publish`.
* No credentials? `docker compose up --build` runs everything against **LocalStack**
  (fake AWS). Read the delivered messages with
  `awslocal logs tail /log-anomaly-detector/alerts` and
  `awslocal sqs receive-message --queue-url http://localhost:4566/000000000000/alert-inbox`.

If AWS is slow or down, the WebSocket feed is never delayed (each destination has its own
queue), deliveries are retried with backoff, and anything that still fails is written to
`data/alerts_fallback.jsonl`.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard |
| WS | `/ws/alerts` | Live alerts + incident updates |
| WS | `/ws/metrics` | Live metrics every tick |
| GET | `/alerts?limit&min_severity&status&incident_id` | Alert history |
| GET | `/alerts/{id}` | One alert |
| POST | `/alerts/{id}/feedback` `{"label":"TP"\|"FP"}` | Mark true/false positive |
| GET | `/incidents`, `/incidents/{id}` | Grouped alerts; one incident with owner, action log, latest alert |
| POST | `/incidents/{id}/ack` `{"operator"}` | Take ownership / reassign |
| POST | `/incidents/{id}/actions` `{"operator","action","source","rec_id"}` | Log an action |
| POST | `/incidents/{id}/resolution` `{"operator","fixed"}` | "Did your actions fix it?" (learning) |
| GET | `/learning/resolutions` | Everything the learning runbook has recorded |
| POST | `/demo/break-model` `{"name"}` | Simulate a detector failure (degraded mode) |
| GET | `/metrics`, `/metrics/history?minutes=15` | Error rate, baseline, scores |
| GET | `/health` | Pipeline, models, publisher stats |
| GET | `/benchmark`, `/models`; POST `/models/reload` | Model comparison, hot reload |
| GET | `/demo/scenarios`; POST `/demo/inject-anomaly` | Demo injection |

Full contract with payload examples: `docs/api-contract.md`. Interactive docs at `/docs`.

## Tests
```bash
cd backend && pytest -q        # 35 tests: core logic, tailer, AWS (moto), dispatcher, ML, API + WebSocket
```

## Project layout
```
backend/app/
  main.py, config.py
  api/          REST routes + WebSockets
  core/         tailer, parsers, template miner, window, baseline, detector, severity,
                alert manager, incident grouper, pipeline
  ml/           features, Isolation Forest, LSTM-AE, calibration, ensemble, train, evaluate, registry
  explain/      root-cause ranking of log templates
  publishers/   websocket, cloudwatch, sns, local_file, dispatcher
  db/           SQLite store
  sim/          log simulator with injectable anomaly scenarios
backend/artifacts/   trained models + calibration
backend/tests/
frontend/index.html  dashboard (single file, no build step)
scripts/             generate_logs, generate_training_data, run_benchmark, replay_logs, demo.sh
docs/                architecture, API contract, model card, demo script
ml_research/         benchmark results
infra/localstack/    fake-AWS bootstrap
```

## Results (SIMULATED data)

See `ml_research/benchmark_results.md`. On the simulated test set, the deployed ensemble
catches all 7 injected anomalies (the z-score alone catches 5 of 7 and misses both volume
anomalies) with a mean time-to-detect of ~17 s and no false-alarm episodes in the
1.5 h of normal test traffic. These numbers come from the simulator and
say nothing about real production logs. The model card (`docs/model-card.md`) lists
the limitations.
