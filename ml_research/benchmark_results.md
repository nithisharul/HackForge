# Benchmark (SIMULATED data from app/sim/simulator.py)

Windows scored: 1375 (318 anomalous). A detector fires at normalised score >= 1.0.

| Detector | Precision | Recall | F1 | ROC-AUC | False alarms/h | FP windows/h | Detected | Mean TTD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Z-score (statistical) | 1.0 | 0.676 | 0.807 | 0.806 | 0.0 | 0.0 | 5/7 | 25.0 s |
| Isolation Forest | 0.99 | 0.619 | 0.762 | 0.982 | 1.36 | 1.36 | 7/7 | 33.6 s |
| LSTM-Autoencoder | 0.858 | 0.969 | 0.91 | 0.982 | 6.81 | 34.74 | 7/7 | 15.0 s |
| Ensemble (deployed) | 0.919 | 0.962 | 0.94 | 0.986 | 0.0 | 18.39 | 7/7 | 17.2 s |

## Time-to-detect by scenario (seconds, '-' = missed)

| Scenario | Z-score (statistical) | Isolation Forest | LSTM-Autoencoder | Ensemble (deployed) |
|---|---:|---:|---:|---:|
| error_spike | 10, 5 | 5, 5 | 5, 5 | 5, 5 |
| slow_ramp | 80 | 60 | 65 | 65 |
| volume_surge | - | 80 | 5 | 10 |
| volume_drop | - | 35 | 15 | 25 |
| new_errors | 25 | 15 | 5 | 5 |
| fatal_burst | 5 | 35 | 5 | 5 |

False alarms/h counts alert *episodes* that never touch an injected anomaly. FP windows/h also counts the short recovery tail right after an anomaly ends.

All numbers are on SIMULATED data from app/sim/simulator.py - not real production logs.
