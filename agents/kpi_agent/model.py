"""KPI classifier model (spec Appendix D.1, D.2)."""
import torch
import torch.nn as nn

SEQ_LEN = 6        # 6 x 10s = 60s window
N_FEATURES = 9
N_CLASSES = 5

CLASS_NAMES = ["NORMAL", "OVERLOAD", "UNDERLOAD", "SINR_LOW", "POWER_WASTE"]

# feature order: prb_dl_pct, sinr_db, connected_ues, power_w, packet_loss_pct,
#                dl_throughput_mbps, cqi, bler_pct, latency_ms
FEATURE_NAMES = [
    "prb_dl_pct", "sinr_db", "connected_ues", "power_w", "packet_loss_pct",
    "dl_throughput_mbps", "cqi", "bler_pct", "latency_ms",
]
# (min, range) per feature
FEATURE_NORM = [
    (0.0, 100.0),
    (-5.0, 35.0),
    (0.0, 800.0),
    (0.0, 1200.0),
    (0.0, 5.0),
    (0.0, 4000.0),
    (0.0, 15.0),
    (0.0, 30.0),
    (0.0, 500.0),
]


def normalise(raw):
    """raw: sequence (list of 9-feature vectors) -> normalised (not clipped)."""
    return [[(v - FEATURE_NORM[i][0]) / FEATURE_NORM[i][1] for i, v in enumerate(step)]
            for step in raw]


class KPIClassifier(nn.Module):
    def __init__(self):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=N_FEATURES, hidden_size=64, num_layers=2,
            batch_first=True, dropout=0.25, bidirectional=True,
        )
        self.head = nn.Sequential(
            nn.Linear(128, 64),   # 128 = hidden*2 (bidirectional)
            nn.ReLU(),
            nn.Dropout(0.25),
            nn.Linear(64, N_CLASSES),
        )

    def forward(self, x):
        out, _ = self.lstm(x)
        return self.head(out[:, -1, :])   # last timestep only
