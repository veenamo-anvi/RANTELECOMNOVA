"""Synthetic training for the KPI classifier (spec Appendix D.3, D.4).

Reproducible (np.random.seed(0)); writes kpi_model.pt. Trained on first boot if the
model file is absent.
"""
import os

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset, WeightedRandomSampler

from model import KPIClassifier, SEQ_LEN, N_FEATURES, N_CLASSES, FEATURE_NORM

EPOCHS = 60
BATCH_SIZE = 256
LR = 1e-3
MODEL_PATH = os.getenv("MODEL_PATH", "kpi_model.pt")

# class -> count
CLASS_COUNTS = {0: 3500, 1: 750, 2: 400, 3: 250, 4: 100}

# per-class means/stds, order = the 9 features. [0]=5G, [1]=4G
_5G_MEANS = {
    0: [55, 20, 350, 520, 0.05, 1400, 11, 1.5, 12],
    1: [94, 11, 720, 940, 0.85, 3100, 7, 8.0, 38],
    2: [9, 24, 20, 330, 0.01, 190, 14, 0.3, 9],
    3: [54, 1, 290, 580, 1.60, 720, 3, 12.0, 45],
    4: [13, 24, 8, 880, 0.01, 145, 14, 0.2, 9],
}
_4G_MEANS = {
    0: [48, 22, 130, 120, 0.04, 110, 10, 1.2, 15],
    1: [92, 12, 230, 195, 0.75, 140, 6, 7.0, 52],
    2: [8, 25, 10, 65, 0.01, 18, 13, 0.2, 11],
    3: [50, 0, 120, 140, 1.40, 85, 3, 10.0, 60],
    4: [10, 26, 5, 175, 0.01, 12, 13, 0.1, 10],
}
_5G_STDS = {
    0: [14, 4, 130, 200, 0.05, 500, 2, 0.8, 4],
    1: [3, 3, 70, 55, 0.40, 200, 2, 2.5, 8],
    2: [4, 5, 8, 120, 0.01, 100, 1, 0.2, 2],
    3: [20, 2, 100, 200, 0.80, 300, 2, 3.0, 12],
    4: [5, 5, 3, 100, 0.01, 60, 1, 0.1, 2],
}
_4G_STDS = {
    0: [12, 4, 50, 45, 0.04, 40, 2, 0.6, 5],
    1: [4, 3, 20, 10, 0.35, 10, 2, 2.0, 12],
    2: [3, 5, 4, 20, 0.01, 8, 1, 0.1, 3],
    3: [18, 2, 45, 50, 0.70, 30, 2, 2.5, 15],
    4: [4, 5, 2, 25, 0.01, 5, 1, 0.05, 2],
}


def _normalise_seq(seq):
    out = np.empty_like(seq)
    for i in range(N_FEATURES):
        mn, rng = FEATURE_NORM[i]
        out[:, i] = (seq[:, i] - mn) / rng
    return out


def _make_sequence(means, stds):
    means = np.array(means, dtype=np.float64)
    stds = np.array(stds, dtype=np.float64)
    base = means + np.random.randn(N_FEATURES) * stds * 0.5
    seq = np.empty((SEQ_LEN, N_FEATURES), dtype=np.float64)
    for t in range(SEQ_LEN):
        seq[t] = base + np.random.randn(N_FEATURES) * stds * 0.15
        base = base + np.random.randn(N_FEATURES) * stds * 0.05   # slow drift
    return _normalise_seq(seq)


def build_dataset():
    X, y = [], []
    for cls, count in CLASS_COUNTS.items():
        for specs_m, specs_s in ((_5G_MEANS, _5G_STDS), (_4G_MEANS, _4G_STDS)):
            for _ in range(count):   # split evenly between 5G and 4G sub-profiles
                X.append(_make_sequence(specs_m[cls], specs_s[cls]))
                y.append(cls)
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int64)


def train(model_path=MODEL_PATH):
    np.random.seed(0)
    torch.manual_seed(0)
    X, y = build_dataset()

    n = len(X)
    idx = np.random.permutation(n)
    split = int(n * 0.8)
    tr, va = idx[:split], idx[split:]

    Xtr = torch.tensor(X[tr]); ytr = torch.tensor(y[tr])
    Xva = torch.tensor(X[va]); yva = torch.tensor(y[va])

    # WeightedRandomSampler — inverse class frequency
    counts = np.bincount(y[tr], minlength=N_CLASSES)
    w_per_class = 1.0 / np.maximum(counts, 1)
    sample_w = w_per_class[y[tr]]
    sampler = WeightedRandomSampler(torch.as_tensor(sample_w, dtype=torch.double),
                                    num_samples=len(sample_w), replacement=True)
    loader = DataLoader(TensorDataset(Xtr, ytr), batch_size=BATCH_SIZE, sampler=sampler)

    model = KPIClassifier()
    opt = torch.optim.Adam(model.parameters(), lr=LR)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS)
    loss_fn = nn.CrossEntropyLoss()

    for epoch in range(EPOCHS):
        model.train()
        for xb, yb in loader:
            opt.zero_grad()
            out = model(xb)
            loss = loss_fn(out, yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        sched.step()
        if (epoch + 1) % 10 == 0:
            model.eval()
            with torch.no_grad():
                acc = (model(Xva).argmax(1) == yva).float().mean().item()
            print(f"[train] epoch {epoch + 1}/{EPOCHS} val_acc={acc:.3f}", flush=True)

    torch.save(model.state_dict(), model_path)
    print(f"[train] saved {model_path}", flush=True)
    return model


if __name__ == "__main__":
    train()
