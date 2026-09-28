# ml_research

* `benchmark_results.json` / `.md` – output of `scripts/run_benchmark.py` (SIMULATED data).
* Reproduce: `python scripts/generate_training_data.py && (cd backend && python -m app.ml.train --data ../data/train_normal.log) && python scripts/run_benchmark.py`
* Real data: download BGL from LogHub (https://github.com/logpai/loghub), split a normal
  period for training (`--format bgl`), and build a labels file from the first column
  (non "-" lines are alerts). Report those numbers separately and label them "real (BGL)".
