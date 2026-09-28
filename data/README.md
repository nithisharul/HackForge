# data/

Generated at runtime (not shipped, recreate in ~3 s):

* `app.log` – the live file the detector tails (written by `scripts/generate_logs.py` or the built-in simulator)
* `train_normal.log`, `test_labeled.log`, `test_labels.json` – `python scripts/generate_training_data.py` (SIMULATED)
* `alerts_fallback.jsonl` – alerts written locally when AWS is off or unreachable

`sample.log` shows the expected line format.
