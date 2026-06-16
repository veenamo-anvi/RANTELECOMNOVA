"""KPI monitoring agent (spec §6.4 + Appendix D.5-D.9).

Polls InfluxDB, maintains a 6-step sliding window per cell, classifies via rule
fallback (window filling) then the BiLSTM, and takes autonomous SON actions.
"""
import os
import time
from collections import defaultdict, deque

import httpx
import torch
import torch.nn.functional as F

from model import KPIClassifier, normalise, SEQ_LEN, CLASS_NAMES, FEATURE_NAMES
import train as trainer

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "telecom")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "telecom_metrics")
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://controller:8080")

POLL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "10"))
OVERLOAD_PRB = float(os.getenv("OVERLOAD_PRB_PCT", "85"))
UNDERLOAD_PRB = float(os.getenv("UNDERLOAD_PRB_PCT", "20"))
SINR_MIN_DB = float(os.getenv("SINR_MIN_DB", "5"))
POWER_WASTE_W = float(os.getenv("POWER_WASTE_W", "500"))
POWER_WASTE_UE = float(os.getenv("POWER_WASTE_MIN_UES", "15"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.70"))
MODEL_PATH = os.getenv("MODEL_PATH", "kpi_model.pt")

_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
_write_api = _client.write_api(write_options=SYNCHRONOUS)
_query_api = _client.query_api()

_windows = defaultdict(lambda: deque(maxlen=SEQ_LEN))
_last_moved = defaultdict(float)


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------
def load_model():
    model = KPIClassifier()
    if os.path.exists(MODEL_PATH):
        model.load_state_dict(torch.load(MODEL_PATH, map_location="cpu"))
        print(f"[kpi] loaded {MODEL_PATH}", flush=True)
    else:
        print("[kpi] model absent — training on first boot...", flush=True)
        model = trainer.train(MODEL_PATH)
    model.eval()
    return model


# --------------------------------------------------------------------------
# influx
# --------------------------------------------------------------------------
def latest_cells():
    """Latest cell_kpi per cell over -3m, pivoted by field (D.7)."""
    flux = (
        f'from(bucket: "{INFLUX_BUCKET}") '
        f'|> range(start: -3m) '
        f'|> filter(fn: (r) => r._measurement == "cell_kpi") '
        f'|> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")'
    )
    latest = {}
    try:
        for table in _query_api.query(flux):
            for rec in table.records:
                v = rec.values
                cid = v.get("cell_id")
                if not cid:
                    continue
                t = rec.get_time()
                if cid not in latest or t > latest[cid][0]:
                    latest[cid] = (t, v)
    except Exception as e:  # noqa: BLE001
        print(f"[kpi] query failed: {e}", flush=True)
    return {cid: v for cid, (_, v) in latest.items()}


def feature_vector(vals):
    return [float(vals.get(f, 0.0) or 0.0) for f in FEATURE_NAMES]


def write_alert(severity, alert_type, cell_id, du_id, message, metric_value=None,
                threshold=None, ai_confidence=None):
    p = (Point("alerts").tag("severity", severity).tag("cell_id", cell_id)
         .tag("du_id", du_id or "").tag("alert_type", alert_type)
         .field("message", message))
    if metric_value is not None:
        p = p.field("metric_value", float(metric_value))
    if threshold is not None:
        p = p.field("threshold", float(threshold))
    if ai_confidence is not None:
        p = p.field("ai_confidence", float(ai_confidence))
    _safe_write(p)


def write_son(action_type, cell_id, du_id, message, confidence):
    p = (Point("son_actions").tag("cell_id", cell_id).tag("du_id", du_id or "")
         .tag("action_type", action_type)
         .field("message", message).field("confidence", float(confidence)))
    _safe_write(p)


def _safe_write(point):
    try:
        _write_api.write(bucket=INFLUX_BUCKET, record=point)
    except Exception as e:  # noqa: BLE001
        print(f"[kpi] write failed: {e}", flush=True)


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------
def rule_classify(vals):
    prb = float(vals.get("prb_dl_pct", 0) or 0)
    sinr = float(vals.get("sinr_db", 99) or 99)
    power = float(vals.get("power_w", 0) or 0)
    ues = float(vals.get("connected_ues", 0) or 0)
    if prb > OVERLOAD_PRB:
        return "OVERLOAD"
    if prb < UNDERLOAD_PRB:
        return "UNDERLOAD"
    if sinr < SINR_MIN_DB:
        return "SINR_LOW"
    if power > POWER_WASTE_W and ues < POWER_WASTE_UE:
        return "POWER_WASTE"
    return "NORMAL"


def ai_classify(model, window):
    seq = normalise(list(window))
    x = torch.tensor([seq], dtype=torch.float32)
    with torch.no_grad():
        probs = F.softmax(model(x), dim=1)[0]
    idx = int(probs.argmax())
    return CLASS_NAMES[idx], float(probs[idx])


# --------------------------------------------------------------------------
# SON actions (D.8)
# --------------------------------------------------------------------------
def du_avg_prb(cells):
    sums, counts = defaultdict(float), defaultdict(int)
    for v in cells.values():
        did = v.get("du_id")
        if did:
            sums[did] += float(v.get("prb_dl_pct", 0) or 0)
            counts[did] += 1
    return {d: sums[d] / counts[d] for d in sums if counts[d]}


def handle_overload(cell_id, vals, du_avg, confidence):
    du_id = vals.get("du_id")
    prb = float(vals.get("prb_dl_pct", 0) or 0)
    write_alert("WARNING", "OVERLOAD", cell_id, du_id,
                f"PRB {prb:.1f}% > {OVERLOAD_PRB}", prb, OVERLOAD_PRB, confidence)
    # pick least-loaded DU != current with du_avg < OVERLOAD_PRB - 20
    candidates = {d: a for d, a in du_avg.items()
                  if d != du_id and a < OVERLOAD_PRB - 20}
    if not candidates:
        return
    target = min(candidates, key=candidates.get)
    if time.time() - _last_moved[cell_id] < POLL_SEC * 3:
        return  # cooldown
    try:
        r = httpx.post(f"{CONTROLLER_URL}/move/cell",
                       json={"cell_id": cell_id, "to_du_id": target}, timeout=10)
        r.raise_for_status()
        _last_moved[cell_id] = time.time()
        write_alert("INFO", "LOAD_BALANCE", cell_id, du_id,
                    f"moved {cell_id} -> {target}", prb, OVERLOAD_PRB, confidence)
        write_son("LOAD_BALANCE", cell_id, du_id,
                  f"moved to least-loaded DU {target}", confidence)
    except httpx.HTTPError as e:
        print(f"[kpi] move failed: {e}", flush=True)


def handle_underload(cell_id, vals, du_avg, confidence):
    du_id = vals.get("du_id")
    prb = float(vals.get("prb_dl_pct", 0) or 0)
    write_alert("INFO", "UNDERLOAD", cell_id, du_id,
                f"PRB {prb:.1f}% < {UNDERLOAD_PRB}", prb, UNDERLOAD_PRB, confidence)
    others = {d: a for d, a in du_avg.items() if d != du_id}
    target = min(others, key=others.get) if others else du_id
    write_son("TRAFFIC_STEER", cell_id, du_id,
              f"recommend handover to min-PRB DU {target}; enable sleep/DTX", confidence)


def handle_sinr_low(cell_id, vals, confidence):
    du_id = vals.get("du_id")
    sinr = float(vals.get("sinr_db", 0) or 0)
    write_alert("CRITICAL", "SINR_DEGRADATION", cell_id, du_id,
                f"SINR {sinr:.1f} dB < {SINR_MIN_DB}", sinr, SINR_MIN_DB, confidence)
    try:
        httpx.post(f"{CONTROLLER_URL}/son/pci-reopt",
                   json={"cell_id": cell_id}, timeout=10)
    except httpx.HTTPError as e:
        print(f"[kpi] pci-reopt best-effort failed: {e}", flush=True)
    write_son("PCI_REOPT_REQUEST", cell_id, du_id,
              "requested collision-free PCI re-optimisation", confidence)


def handle_power_waste(cell_id, vals, confidence):
    du_id = vals.get("du_id")
    power = float(vals.get("power_w", 0) or 0)
    write_alert("WARNING", "POWER_WASTE", cell_id, du_id,
                f"power {power:.0f}W with few UEs", power, POWER_WASTE_W, confidence)
    write_son("DTX_RECOMMEND", cell_id, du_id,
              f"recommend DTX; est saving {power * 0.35:.0f}W", confidence)


def act(cell_id, label, vals, du_avg, confidence):
    if label == "OVERLOAD":
        handle_overload(cell_id, vals, du_avg, confidence)
    elif label == "UNDERLOAD":
        handle_underload(cell_id, vals, du_avg, confidence)
    elif label == "SINR_LOW":
        handle_sinr_low(cell_id, vals, confidence)
    elif label == "POWER_WASTE":
        handle_power_waste(cell_id, vals, confidence)


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------
def main():
    print(f"[kpi] starting; poll={POLL_SEC}s conf>={MIN_CONFIDENCE}", flush=True)
    model = load_model()
    while True:
        cells = latest_cells()
        du_avg = du_avg_prb(cells)
        for cid, vals in cells.items():
            _windows[cid].append(feature_vector(vals))
            window = _windows[cid]
            if len(window) < SEQ_LEN:
                label, source, conf = rule_classify(vals), "RULE", 1.0
            else:
                label, conf = ai_classify(model, window)
                source = "AI"
            if label == "NORMAL":
                continue
            # act gate: RULE always, AI only if confident
            if source == "RULE" or conf >= MIN_CONFIDENCE:
                act(cid, label, vals, du_avg, conf)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
