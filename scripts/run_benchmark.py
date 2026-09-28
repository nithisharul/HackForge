#!/usr/bin/env python3
"""Produce the model comparison table (z-score vs Isolation Forest vs LSTM-AE vs ensemble).

    python scripts/run_benchmark.py            # uses data/test_labeled.log (SIMULATED)

Writes ml_research/benchmark_results.json (served at GET /benchmark) and
ml_research/benchmark_results.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.ml.evaluate import evaluate  # noqa: E402

NAMES = {"zscore": "Z-score (statistical)", "iforest": "Isolation Forest",
         "lstm_ae": "LSTM-Autoencoder", "ensemble": "Ensemble (deployed)"}


def to_markdown(res: dict) -> str:
    out = [f"# Benchmark ({res['data']})", "",
           f"Windows scored: {res['windows_scored']} ({res['positives']} anomalous). "
           "A detector fires at normalised score >= 1.0.", "",
           "| Detector | Precision | Recall | F1 | ROC-AUC | False alarms/h | FP windows/h | Detected | Mean TTD |",
           "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for k, v in res["results"].items():
        ttd = f"{v['mean_time_to_detect_s']} s" if v["mean_time_to_detect_s"] is not None else "-"
        out.append(f"| {NAMES.get(k, k)} | {v['precision']} | {v['recall']} | {v['f1']} | {v['roc_auc']} | "
                   f"{v['false_alarm_episodes_per_hour']} | {v['false_positive_windows_per_hour']} | "
                   f"{v['anomalies_detected']} | {ttd} |")
    out += ["", "## Time-to-detect by scenario (seconds, '-' = missed)", "",
            "| Scenario | " + " | ".join(NAMES.get(k, k) for k in res["results"]) + " |",
            "|---|" + "---:|" * len(res["results"])]
    kinds = next(iter(res["results"].values()))["detected_by_kind"].keys()
    for kind in kinds:
        cells = []
        for v in res["results"].values():
            vals = v["detected_by_kind"].get(kind, [])
            cells.append(", ".join("-" if x is None else f"{x:g}" for x in vals))
        out.append(f"| {kind} | " + " | ".join(cells) + " |")
    out += ["", "False alarms/h counts alert *episodes* that never touch an injected anomaly. "
            "FP windows/h also counts the short recovery tail right after an anomaly ends.",
            "", "All numbers are on SIMULATED data from app/sim/simulator.py - not real production logs."]
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--log", type=Path, default=ROOT / "data" / "test_labeled.log")
    p.add_argument("--labels", type=Path, default=ROOT / "data" / "test_labels.json")
    p.add_argument("--artifacts", type=Path, default=settings.artifacts_dir)
    p.add_argument("--format", default="generic")
    a = p.parse_args()
    res = evaluate(a.log, a.labels, a.artifacts, a.format)
    out_dir = ROOT / "ml_research"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "benchmark_results.json").write_text(json.dumps(res, indent=2))
    md = to_markdown(res)
    (out_dir / "benchmark_results.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
