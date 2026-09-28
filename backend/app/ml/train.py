"""Train, calibrate and save the anomaly models.

    cd backend
    python -m app.ml.train --data ../data/train_normal.log

Pipeline: raw log -> same featurizer as live -> scaler -> chronological
train/validation split -> fit Isolation Forest + LSTM-AE on train ->
calibrate each on validation (normal data only) -> write artifacts/.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.config import settings
from app.ml.calibration import Calibration, save_calibrations
from app.ml.features import FeatureScaler, featurize_lines, make_sequences, snapshots_to_matrix
from app.ml.isolation_forest import IsolationForestModel
from app.ml.lstm_autoencoder import TORCH_AVAILABLE, LSTMAutoencoder
from app.models.schemas import FEATURE_NAMES

log = logging.getLogger("train")


def train(data_files: list[Path], out_dir: Path, log_format: str = "generic",
          window_seconds: float | None = None, tick_seconds: float | None = None,
          seq_len: int | None = None, epochs: int = 30, use_lstm: bool = True,
          val_fraction: float = 0.2) -> dict:
    window_seconds = window_seconds or settings.window_seconds
    tick_seconds = tick_seconds or settings.tick_seconds
    seq_len = seq_len or settings.sequence_length
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    snaps = []
    for f in data_files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            snaps += featurize_lines(fh, log_format, window_seconds, tick_seconds,
                                     settings.min_events_per_window)
    warmup_ticks = int(window_seconds / tick_seconds)   # first window has all-new templates
    X_raw = snapshots_to_matrix(snaps, skip_warmup=warmup_ticks)
    if len(X_raw) < 50:
        raise SystemExit(f"Only {len(X_raw)} usable windows - need more normal data.")
    log.info("Featurized %d windows in %.1fs", len(X_raw), time.time() - t0)

    split = int(len(X_raw) * (1 - val_fraction))
    scaler = FeatureScaler.fit(X_raw[:split])
    X = scaler.transform(X_raw)
    X_train, X_val = X[:split], X[split:]
    scaler.save(out_dir / "scaler.json")

    cals: dict[str, Calibration] = {}
    report: dict = {"windows_total": int(len(X)), "windows_train": int(len(X_train)),
                    "windows_val": int(len(X_val)), "models": {}}

    # ---- Isolation Forest -------------------------------------------------
    t = time.time()
    iforest = IsolationForestModel().fit(X_train)
    iforest.save(out_dir)
    cals["iforest"] = Calibration.fit(iforest.score(X_val))
    report["models"]["iforest"] = {"train_seconds": round(time.time() - t, 2),
                                   "calibration": cals["iforest"].__dict__}
    log.info("Isolation Forest trained (%.1fs)", time.time() - t)

    # ---- LSTM Autoencoder -------------------------------------------------
    if use_lstm and TORCH_AVAILABLE:
        t = time.time()
        S_train = make_sequences(X_train, seq_len)
        S_val = make_sequences(X_val, seq_len)
        ae = LSTMAutoencoder(n_features=X.shape[1], seq_len=seq_len, epochs=epochs).fit(S_train)
        ae.save(out_dir)
        cals["lstm_ae"] = Calibration.fit(ae.score(S_val))
        report["models"]["lstm_ae"] = {
            "train_seconds": round(time.time() - t, 2),
            "final_train_loss": round(ae.train_losses[-1], 5),
            "calibration": cals["lstm_ae"].__dict__,
        }
        log.info("LSTM-AE trained (%.1fs), final loss %.4f", time.time() - t, ae.train_losses[-1])
    elif use_lstm:
        log.warning("PyTorch not installed - skipping LSTM-AE")

    save_calibrations(cals, out_dir / "calibration.json")
    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "data_files": [str(f) for f in data_files],
        "log_format": log_format,
        "features": FEATURE_NAMES,
        "window_seconds": window_seconds,
        "tick_seconds": tick_seconds,
        "sequence_length": seq_len,
        "feature_means_raw": np.round(X_raw.mean(axis=0), 4).tolist(),
        **report,
    }
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2))
    return meta


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", nargs="+", type=Path, required=True, help="normal-behaviour log file(s)")
    p.add_argument("--format", default=settings.log_format)
    p.add_argument("--out", type=Path, default=settings.artifacts_dir)
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--no-lstm", action="store_true")
    args = p.parse_args()
    meta = train(args.data, args.out, args.format, epochs=args.epochs, use_lstm=not args.no_lstm)
    print(json.dumps({k: meta[k] for k in ("windows_total", "models")}, indent=2))


if __name__ == "__main__":
    main()
