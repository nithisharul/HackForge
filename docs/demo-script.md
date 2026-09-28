# 5-minute demo script

1. **Start** `./scripts/demo.sh` (or `docker compose up --build`). Open http://localhost:8000.
2. **Explain the screen (while the baseline warms up, ~2 min).** Top tiles show the rolling
   error rate over the last 60 s and the learned baseline. The first chart shows the error rate
   against the blue "normal band" (mean + 3σ). The second chart shows each detector's score on one
   scale, where 1.0 is the edge of normal. Point at the header: "3 detectors: statistical, Isolation Forest, LSTM-AE."
3. **Error spike.** Click `error spike` (2 min). Within ~5–10 s: the line leaves the band, the
   score crosses CRITICAL, and a CRITICAL alert appears in the feed and as a toast. Open "Why?":
   detector scores, top features, the log template that exploded, sample lines.
   Show that repeated windows did *not* create repeated alerts (dedup + cooldown).
4. **Volume drop.** Click `volume drop`. The error rate barely moves (the z-score stays flat) but
   Isolation Forest and LSTM-AE fire. This is the reason for having ML models alongside the z-score.
5. **Resolution.** After the injection ends, a green RESOLVED alert arrives and the incident
   table shows start, duration and peak severity.
6. **AWS.** With docker compose: `awslocal logs tail /log-anomaly-detector/alerts` and
   `awslocal sqs receive-message --queue-url http://localhost:4566/000000000000/alert-inbox`
   show the same alert in CloudWatch Logs and the SNS page.
7. **Evidence.** Scroll to the benchmark table: precision/recall/time-to-detect per detector,
   clearly labelled SIMULATED.
8. **Feedback.** Mark an alert as "False alarm" to show the alert-fatigue tracking.

## Hidden payment failure (payment-scoped incident) — ~4 min

All traffic here is **synthetic**, produced by the built-in `LogSimulator`. The
scenario shows a checkout failure that the overall error rate hides.

What the simulator does: the `[payments]` service emits `POST /api/v1/checkout
<status> <ms>ms request_id=<hex>` completion lines at a fixed 0.5/s (about 30 per
60 s window, ~2.5% of all lines), carved out of the normal 20 lines/s. During
`hidden_payment_failure`, 3 of every 5 checkout requests return HTTP 5xx while all
other services stay healthy. When the injection ends, checkouts return 201 again.

1. **Start** (from the repo root, with the venv set up by `scripts/demo.sh`):

   ```bash
   cd backend
   SIMULATOR_ENABLED=true uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

   Windows PowerShell: `$env:SIMULATOR_ENABLED="true"; ..\.venv\Scripts\python -m uvicorn app.main:app --port 8000`

2. **Open** http://localhost:8000 and **wait ~2.5 min**: the header pill should say
   "Baseline: ready", plus about 1 more minute for the Isolation Forest warm-up. The
   "Payment checkout" card shows about 30 checkout requests per window at a 0.0% failure rate.
3. **Inject.** Click **Inject hidden payment failure** on the payment card (it uses the
   "Duration" select; pick 1 min). Alternatively:

   ```bash
   curl -X POST localhost:8000/demo/inject-anomaly -H 'Content-Type: application/json' \
        -d '{"kind":"hidden_payment_failure","duration_seconds":60}'
   ```

4. **Detection (~20–30 s).** The checkout failure rate climbs past the 20% threshold and
   a **"Payment checkout failure spike"** alert appears in the feed (scope `payments`).
   Point at the top tiles: the *overall* error rate only rises from ~3% to ~4%, and the
   global detector stays at or near normal. The card shows status **OPEN**, the first
   detected time, and up to 5 actual failed checkout lines, labelled as supporting
   evidence, not a proven root cause. No repeat alert is sent while it stays open.
5. **Refresh the page.** The payment incident is reloaded from `GET /incidents`.
6. **Recovery (~60–75 s after the injection ends).** Successful checkouts push the failed
   ones out of the 60 s window. Once the rate is below 5% with at least 20 real requests,
   the card shows **RECOVERING** (healthy window 1/3, 2/3), then **RESOLVED** with a green
   "RESOLVED: Payment checkout failure spike" alert. If checkout traffic stops instead,
   the card says "recovery unverified" and the incident stays open.

Thresholds live in `backend/app/config.py` (`PAYMENT_*` env vars):
`payment_failure_threshold=0.20`, `payment_healthy_rate=0.05`,
`payment_min_requests=20`, `payment_resolve_after_windows=3`.
