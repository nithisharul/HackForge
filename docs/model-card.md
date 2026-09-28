# Model card

## Models

| Model | Input | Output | Trained on |
|---|---|---|---|
| EWMA z-score | window error rate | σ distance from adaptive baseline | nothing (learns online) |
| Isolation Forest (200 trees) | 6 scaled features of one window | −score_samples | 6 h simulated normal traffic |
| LSTM-Autoencoder (LSTM 32 → latent 8 → LSTM 32) | 10 consecutive windows × 6 features | MSE of the last 2 reconstructed windows | same, 30 epochs, Adam 1e-3 |

Features: `log1p(volume)`, `log1p(error_count)`, `error_rate`, `warn_rate`,
`log1p(unique_templates)`, `log1p(new_templates)`, standardised with the training mean/σ.
Split: chronological 80 % train / 20 % calibration (no shuffling across time).

## Calibration

Each model's score on the calibration split gives the median and 99th percentile;
`normalized = (raw − median)/(p99 − median)`. By construction ≈1 % of normal windows score
≥ 1.0 (LOW) per model. Values are in `backend/artifacts/calibration.json`.

## Evaluation data: SIMULATED

`scripts/generate_training_data.py` → 2 h test log with 7 injected anomalies (error spike ×2,
slow ramp, volume surge, volume drop, new error types, fatal burst). A window is positive if
≥ 25 % of it overlaps an anomaly, negative if it has no overlap, ignored otherwise.
Results: `ml_research/benchmark_results.md`.

Headline results: the z-score has perfect precision but misses anomalies that don't change the
error rate (volume surge/drop). The LSTM-AE detects everything fastest but stays elevated for
~30–40 s after an anomaly ends (its input sequence still contains the anomaly), which shows
up as false-positive windows in the "recovery tail". The ensemble keeps the z-score's coverage
of error spikes and adds the ML models' coverage of volume and pattern anomalies.

## Limitations

* **The metrics are simulated.** The simulator is simple (independent lines, a fixed template
  set, a sine-wave load pattern). Real logs have bursts, deploy-time noise and seasonality, so
  expect more false alarms until the models are retrained on your own normal traffic.
* Models learn the absolute traffic level. Retrain when traffic changes a lot, or rely on the
  z-score only (`ENABLE_ML=false`).
* The template miner is a regex masker, not full Drain; very free-form messages can create many
  templates and inflate `new_templates`.
* No seasonality model: a nightly batch job that always raises errors will alert until the
  baseline has adapted.
* Labels for "normal" are assumed; if the training logs contain incidents, the models learn them
  as normal.
* The BGL parser is included for benchmarking on real data, but no BGL results are claimed here.
  Download BGL from LogHub, then `replay_logs.py --format bgl` or evaluate offline.
