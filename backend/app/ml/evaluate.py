"""Offline evaluation of every detector on a labelled log.

    cd backend
    python -m app.ml.evaluate --log ../data/test_labeled.log --labels ../data/test_labels.json

Labelling rule for a window ending at t (covering (t-W, t]):
  * positive  if >= 25% of the window overlaps an injected anomaly
  * negative  if it does not overlap any anomaly
  * ignored   otherwise (the ambiguous edges of an anomaly)

A detector "fires" when its normalised score >= 1.0 (the calibrated edge of
normal). Reports precision / recall / F1, ROC-AUC, false-positive windows per
hour and mean time-to-detect per anomaly.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.metrics import roc_auc_score

from app.config import settings
from app.core.baseline import EWMABaseline
from app.core.detector import Detector
from app.core.severity import SeverityPolicy
from app.ml.features import featurize_lines
from app.ml.registry import ModelBundle, load_bundle


def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def run_detector(log_path: Path, bundle: ModelBundle, log_format: str = "generic") -> list[dict]:
    W, tick = settings.window_seconds, settings.tick_seconds
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        snaps = featurize_lines(fh, log_format, W, tick, settings.min_events_per_window)
    det = Detector(
        EWMABaseline(settings.baseline_alpha, settings.baseline_warmup_ticks, settings.baseline_min_std),
        SeverityPolicy(settings.sev_low, settings.sev_medium, settings.sev_high, settings.sev_critical,
                       settings.critical_error_rate, settings.escalate_after_windows),
        bundle,
        {"zscore": settings.weight_zscore, "iforest": settings.weight_iforest, "lstm_ae": settings.weight_lstm},
        settings.sequence_length,
        ml_warmup_ticks=int(W / tick),
    )
    det.set_bundle(bundle)
    rows = []
    for s in snaps:
        ev = det.evaluate(s)
        row = {"t": s.ts.timestamp(), "ready": ev.baseline_ready, "severity": ev.severity.name,
               "ensemble": ev.final_score, "error_rate": s.error_rate, "volume": s.volume}
        for d in ev.detectors:
            row[d.name] = d.normalized if d.available else None
        rows.append(row)
    return rows


def score_rows(rows: list[dict], anomalies: list[dict], window: float) -> dict:
    min_overlap = 0.25 * window
    labelled = []
    for r in rows:
        if not r["ready"]:
            continue
        ov = max((_overlap(r["t"] - window, r["t"], a["start"], a["end"]) for a in anomalies), default=0)
        if ov >= min_overlap:
            labelled.append((r, 1))
        elif ov == 0:
            labelled.append((r, 0))
    hours = (len([1 for _, y in labelled if y == 0]) * settings.tick_seconds) / 3600

    detectors = ["zscore", "iforest", "lstm_ae", "ensemble"]
    results = {}
    for name in detectors:
        pairs = [(r[name], y) for r, y in labelled if r.get(name) is not None]
        if not pairs:
            continue
        scores = np.array([p[0] for p in pairs])
        y = np.array([p[1] for p in pairs])
        pred = scores >= 1.0
        tp = int((pred & (y == 1)).sum()); fp = int((pred & (y == 0)).sum())
        fn = int((~pred & (y == 1)).sum())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        auc = float(roc_auc_score(y, scores)) if len(set(y)) == 2 else None

        per_anomaly = {}
        ttds = []
        for a in anomalies:
            hit = next((r["t"] - a["start"] for r in rows
                        if r["ready"] and r.get(name) is not None and r[name] >= 1.0
                        and a["start"] <= r["t"] <= a["end"] + window), None)
            per_anomaly.setdefault(a["kind"], []).append(hit)
            if hit is not None:
                ttds.append(hit)
        # False alarm EPISODES: runs of consecutive firing windows that never touch
        # an anomaly. A run that starts inside an anomaly and trails a little past
        # its end (the "recovery tail") is one true alert, as the alert manager
        # would treat it, so it is not counted here.
        false_episodes, in_run, run_touches = 0, False, False
        for r in rows:
            if not r["ready"] or r.get(name) is None:
                continue
            fires = r[name] >= 1.0
            touches = any(_overlap(r["t"] - window, r["t"], a["start"], a["end"]) > 0 for a in anomalies)
            if fires:
                run_touches = run_touches or touches if in_run else touches
                in_run = True
            elif in_run:
                false_episodes += not run_touches
                in_run = False
        if in_run:
            false_episodes += not run_touches

        results[name] = {
            "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
            "roc_auc": round(auc, 3) if auc is not None else None,
            "false_positive_windows_per_hour": round(fp / hours, 2) if hours else None,
            "false_alarm_episodes_per_hour": round(false_episodes / hours, 2) if hours else None,
            "anomalies_detected": f"{len(ttds)}/{len(anomalies)}",
            "mean_time_to_detect_s": round(float(np.mean(ttds)), 1) if ttds else None,
            "detected_by_kind": {k: [None if v is None else round(v, 1) for v in vals]
                                 for k, vals in per_anomaly.items()},
        }
    return {"windows_scored": len(labelled), "positives": sum(y for _, y in labelled),
            "results": results}


def evaluate(log_path: Path, labels_path: Path, artifacts: Path, log_format: str = "generic") -> dict:
    labels = json.loads(labels_path.read_text())
    bundle = load_bundle(artifacts)
    rows = run_detector(log_path, bundle, log_format)
    out = score_rows(rows, labels["anomalies"], settings.window_seconds)
    out["data"] = labels.get("description", "simulated")
    out["models_loaded"] = list(bundle.models)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--artifacts", type=Path, default=settings.artifacts_dir)
    p.add_argument("--format", default=settings.log_format)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    res = evaluate(a.log, a.labels, a.artifacts, a.format)
    text = json.dumps(res, indent=2)
    if a.out:
        a.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
