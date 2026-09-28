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
| GET | `/incidents` | Grouped alerts |
| GET | `/metrics`, `/metrics/history?minutes=15` | Error rate, baseline, scores |
| GET | `/health` | Pipeline, models, publisher stats |
| GET | `/benchmark`, `/models`; POST `/models/reload` | Model comparison, hot reload |
| GET | `/demo/scenarios`; POST `/demo/inject-anomaly` | Demo injection |

Payment-scoped detector: besides the global detector, checkout requests from `[payments]` are tracked separately (HTTP 5xx = failed). The `hidden_payment_failure` demo scenario (synthetic) is walked through in [docs/demo-script.md](docs/demo-script.md#hidden-payment-failure-payment-scoped-incident--4-min).

Full contract with payload examples: `docs/api-contract.md`. Interactive docs at `/docs`.

## Tests
```bash
cd backend && pytest -q        # 20 tests: core logic, tailer, AWS (moto), dispatcher, ML, API + WebSocket
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
