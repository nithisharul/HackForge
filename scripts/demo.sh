#!/usr/bin/env bash
# One-command local demo (no Docker, no AWS needed).
#   ./scripts/demo.sh
# 1. installs dependencies   2. generates simulated training data (if missing)
# 3. trains the models (if missing)   4. runs the benchmark   5. starts the server
#    with the built-in log simulator. Open http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  $PY -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q torch --index-url https://download.pytorch.org/whl/cpu || echo "torch install failed - continuing without LSTM-AE"
pip install -q -r backend/requirements.txt || pip install -q $(grep -v '^torch' backend/requirements.txt | grep -v '^#' | xargs)

[ -f data/train_normal.log ] || python scripts/generate_training_data.py
[ -f backend/artifacts/scaler.json ] || (cd backend && python -m app.ml.train --data ../data/train_normal.log)
[ -f ml_research/benchmark_results.json ] || python scripts/run_benchmark.py

echo
echo "Dashboard: http://localhost:8000    API docs: http://localhost:8000/docs"
echo "Baseline warms up for ~2 minutes, then use the 'inject' buttons."
cd backend
SIMULATOR_ENABLED=true exec uvicorn app.main:app --host 0.0.0.0 --port 8000
