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
