"""LSTM-Autoencoder over a sequence of consecutive windows.

The encoder LSTM compresses a sequence of `seq_len` feature vectors into one
hidden vector; the decoder LSTM tries to rebuild the whole sequence from it.
Trained only on normal sequences, it rebuilds normal behaviour well and
abnormal behaviour badly, so reconstruction error is the anomaly score.
The per-feature error of the latest window tells us *which* feature is off,
which powers the explanation shown on each alert.

PyTorch is optional: if it is not installed this model simply reports itself
unavailable and the ensemble carries on without it.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from app.ml.base import AnomalyModel

try:
    import torch
    from torch import nn
    TORCH_AVAILABLE = True
except ImportError:  # pragma: no cover
    TORCH_AVAILABLE = False

WEIGHTS = "lstm_ae.pt"
META = "lstm_ae.json"


if TORCH_AVAILABLE:
    class _Net(nn.Module):
        def __init__(self, n_features: int, hidden: int, latent: int):
            super().__init__()
            self.encoder = nn.LSTM(n_features, hidden, batch_first=True)
            self.to_latent = nn.Linear(hidden, latent)
            self.from_latent = nn.Linear(latent, hidden)
            self.decoder = nn.LSTM(hidden, hidden, batch_first=True)
            self.out = nn.Linear(hidden, n_features)

        def forward(self, x):  # x: (b, seq, f)
            _, (h, _) = self.encoder(x)
            z = self.to_latent(h[-1])
            dec_in = self.from_latent(z).unsqueeze(1).repeat(1, x.size(1), 1)
            dec_out, _ = self.decoder(dec_in)
            return self.out(dec_out)


class LSTMAutoencoder(AnomalyModel):
    name = "lstm_ae"

    def __init__(self, n_features: int = 6, seq_len: int = 10, hidden: int = 32,
                 latent: int = 8, epochs: int = 30, lr: float = 1e-3, batch_size: int = 64,
                 seed: int = 42):
        if not TORCH_AVAILABLE:
            raise RuntimeError("PyTorch is not installed; LSTM-AE unavailable")
        self.sequence_length = seq_len
        self.score_last = 2
        self.cfg = dict(n_features=n_features, seq_len=seq_len, hidden=hidden, latent=latent)
        self.epochs, self.lr, self.batch_size = epochs, lr, batch_size
        torch.manual_seed(seed)
        self.net = _Net(n_features, hidden, latent)
        self.train_losses: list[float] = []

    # -- training ------------------------------------------------------------
    def fit(self, X: np.ndarray) -> "LSTMAutoencoder":
        data = torch.tensor(X, dtype=torch.float32)
        opt = torch.optim.Adam(self.net.parameters(), lr=self.lr)
        loss_fn = nn.MSELoss()
        self.net.train()
        for _epoch in range(self.epochs):
            perm = torch.randperm(len(data))
            total = 0.0
            for i in range(0, len(data), self.batch_size):
                batch = data[perm[i:i + self.batch_size]]
                opt.zero_grad()
                loss = loss_fn(self.net(batch), batch)
                loss.backward()
                opt.step()
                total += loss.item() * len(batch)
            self.train_losses.append(total / max(len(data), 1))
        self.net.eval()
        return self

    # -- inference -----------------------------------------------------------
    def _errors(self, X: np.ndarray) -> np.ndarray:
        """Squared reconstruction error, shape (n, seq, f)."""
        X = np.asarray(X, dtype=np.float32)
        if X.ndim == 2:
            X = X[None]
        with torch.no_grad():
            t = torch.from_numpy(X)
            recon = self.net(t).numpy()
        return (recon - X) ** 2

    def score(self, X: np.ndarray) -> np.ndarray:
        err = self._errors(X)
        # Score the most recent windows: the earlier ones give the model context,
        # but scoring them would keep the alert firing long after recovery.
        return err[:, -self.score_last:, :].mean(axis=(1, 2))

    def feature_contributions(self, X: np.ndarray) -> np.ndarray | None:
        err = self._errors(X)[-1, -1, :]        # latest window of last sequence
        total = err.sum()
        return err / total if total > 0 else err

    # -- persistence ---------------------------------------------------------
    def save(self, directory: Path) -> None:
        torch.save(self.net.state_dict(), directory / WEIGHTS)
        (directory / META).write_text(json.dumps({**self.cfg, "train_losses": self.train_losses}))

    @classmethod
    def load(cls, directory: Path) -> "LSTMAutoencoder":
        meta = json.loads((directory / META).read_text())
        obj = cls(n_features=meta["n_features"], seq_len=meta["seq_len"],
                  hidden=meta["hidden"], latent=meta["latent"])
        obj.net.load_state_dict(torch.load(directory / WEIGHTS, map_location="cpu"))
        obj.net.eval()
        obj.train_losses = meta.get("train_losses", [])
        return obj

    @staticmethod
    def exists(directory: Path) -> bool:
        return TORCH_AVAILABLE and (directory / WEIGHTS).exists() and (directory / META).exists()
