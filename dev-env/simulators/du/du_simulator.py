"""DU simulator (spec Appendix C.1).

Reads topology.json, serves the cells currently listed under its DU_ID, and every
INTERVAL_SEC pushes cell_kpi / du_kpi / ue_mobility / ue_usage to InfluxDB.
Re-reads topology every TOPO_POLL_SEC so SON moves / plan-apply take effect live.
"""
import json
import math
import os
import random
import time

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# --- env -------------------------------------------------------------------
INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "telecom")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "telecom_metrics")
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "10"))
TOPO_POLL_SEC = int(os.getenv("TOPO_POLL_SEC", "5"))
TOPOLOGY_FILE = os.getenv("TOPOLOGY_FILE", "/config/topology.json")
DU_ID = os.getenv("DU_ID", "DU-MLS-1")

# --- RF / propagation constants (C.1) --------------------------------------
HB_M = 25.0            # BS height
UE_H_M = 1.5           # UE height
DENSE_URBAN_DB = 3.0   # dense-urban correction
UE_NF_DB = 7.0         # UE noise figure
EDGE_SNR_DB = -3.0     # coverage-edge SNR threshold

# band -> (freq_mhz, bw_mhz, pen_loss_db)
_BAND_PARAMS = {
    "n78": (3500, 100, 20),
    "n41": (2500, 80, 20),
    "n28": (700, 20, 15),
    "B3": (1800, 20, 18),
    "B40": (2300, 20, 18),
}
_ANT_GAIN = {"64T64R": 24.0, "4T4R": 17.0}
_RF_EFF = {"5G": 0.22, "4G": 0.32}

# per-band baselines -> (sinr_base_db, rsrp_base_dbm)
_SINR_BASE = {"n78": 22.0, "n41": 20.0, "n28": 29.0, "B3": 26.0, "B40": 23.0}
_RSRP_BASE = {"n78": -72, "n41": -74, "n28": -64, "B3": -69, "B40": -73}

# --- population / demand (C.1) ---------------------------------------------
AREA_DENSITY = {"Malleswaram": 9000}
DEFAULT_DENSITY = 700
MARKET_SHARE = 0.40
PEAK_CONCURRENT = 0.40

# diurnal load curve, fraction of peak, index = hour 0-23 (DU curve)
HOURLY_LOAD = [
    0.08, 0.06, 0.05, 0.05, 0.06, 0.12, 0.32, 0.68, 0.88, 0.82, 0.72, 0.66,
    0.64, 0.60, 0.62, 0.68, 0.78, 0.90, 0.95, 1.00, 0.97, 0.88, 0.62, 0.28,
]
WEEKEND_FACTOR = 0.75
# slice mix per UE: 7x eMBB, 2x URLLC, 1x mMTC
SLICE_POOL = ["eMBB"] * 7 + ["URLLC"] * 2 + ["mMTC"] * 1

_ue_counter = 0


def _new_ue(slice_type=None):
    global _ue_counter
    _ue_counter += 1
    return {"id": f"UE-{DU_ID}-{_ue_counter}", "slice": slice_type or random.choice(SLICE_POOL)}


def load_factor():
    lt = time.localtime()
    f = HOURLY_LOAD[lt.tm_hour]
    if lt.tm_wday >= 5:  # Sat/Sun
        f *= WEEKEND_FACTOR
    return f


def coverage_radius_m(cell):
    """COST-231-Hata urban-macro coverage radius (C.1)."""
    band = cell["band"]
    freq_mhz, bw_mhz, pen_loss = _BAND_PARAMS.get(band, (cell["freq_mhz"], 20, 18))
    ant_gain = _ANT_GAIN.get(cell["antenna_config"], 17.0)
    rf_eff = _RF_EFF.get(cell["generation"], 0.25)

    rf_w = max(cell["tx_power_w"] * rf_eff, 0.1)
    eirp_dbm = 10 * math.log10(rf_w * 1000) + ant_gain
    noise_dbm = -174 + 10 * math.log10(bw_mhz * 1e6) + UE_NF_DB
    pl_max = eirp_dbm - (noise_dbm - DENSE_URBAN_DB) - pen_loss
    A = 46.3 + 33.9 * math.log10(freq_mhz) - 13.82 * math.log10(HB_M) + DENSE_URBAN_DB
    B = 44.9 - 6.55 * math.log10(HB_M)
    radius_km = 10 ** ((pl_max - A) / B)
    return radius_km * 1000


def expected_peak_ues(cell):
    radius_m = coverage_radius_m(cell)
    density = AREA_DENSITY.get(cell.get("area"), DEFAULT_DENSITY)
    area_km2 = math.pi * (radius_m / 1000) ** 2
    return min(area_km2 * density * MARKET_SHARE * PEAK_CONCURRENT, cell["max_ues"])


class CellState:
    def __init__(self, cell_id, cfg):
        self.cell_id = cell_id
        self.cfg = cfg
        self.peak = expected_peak_ues(cfg)
        # initial pool U(0.8, 1.0) of expected peak at current load
        init = int(self.peak * load_factor() * random.uniform(0.8, 1.0))
        init = max(0, min(init, cfg["max_ues"]))
        self.ues = [_new_ue() for _ in range(init)]

    def update_cfg(self, cfg):
        if cfg != self.cfg:
            self.cfg = cfg
            self.peak = expected_peak_ues(cfg)

    def step_population(self):
        target = int(self.peak * load_factor() * random.uniform(0.88, 1.05))
        target = max(0, min(target, self.cfg["max_ues"]))
        cur = len(self.ues)
        if target > cur:
            self.ues.extend(_new_ue() for _ in range(target - cur))
        elif target < cur:
            del self.ues[target:]
        return len(self.ues)


def _u(a, b):
    return random.uniform(a, b)


def _n(mu, sigma):
    return random.gauss(mu, sigma)


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def cell_kpi_point(state):
    cfg = state.cfg
    connected = len(state.ues)
    max_ues = cfg["max_ues"]
    load = connected / max_ues if max_ues else 0.0
    band = cfg["band"]
    peak_dl = cfg["peak_dl_mbps"]
    tx = cfg["tx_power_w"]
    idle = cfg["idle_power_w"]
    sinr_base = _SINR_BASE.get(band, 22.0)
    rsrp_base = _RSRP_BASE.get(band, -72)

    prb_dl = min(98, load * 100 * _u(0.92, 1.08))
    prb_ul = min(95, load * 58 * _u(0.88, 1.12))
    dl_tp = prb_dl / 100 * peak_dl * _u(0.82, 1.18)
    ul_tp = prb_ul / 100 * peak_dl * 0.22 * _u(0.80, 1.20)
    sinr = sinr_base - load * 15 + _n(0, 2.5)
    rsrp = rsrp_base - load * 22 + _n(0, 3.0)
    rsrq = _clip(-10 + sinr * 0.3 + _n(0, 1.5), -19.5, -3.0)
    cqi = _clip(int((sinr + 5) / 2.5 + _n(0, 0.8)), 0, 15)
    mcs = _clip(int(cqi * 1.8 + _n(0, 1.2)), 0, 28)
    bler = max(0, (load - 0.75) * 15 + (10 - cqi) * 0.5 + _n(0, 0.5))
    power = max(idle * 0.90, idle + load * (tx - idle) + _n(0, tx * 0.025))
    ploss = max(0, (load - 0.75) * 2.5 + _n(0, 0.05))
    latency = max(1, 8 + load * 25 + max(0, 5 - sinr) * 2 + _n(0, 2))
    jitter = max(0.1, latency * _u(0.05, 0.15) + _n(0, 0.3))
    interference = -100 + load * 20 + _n(0, 3)
    ho_sr = _u(0.962, 0.9995)

    p = (
        Point("cell_kpi")
        .tag("cell_id", state.cell_id)
        .tag("area", cfg.get("area", "Malleswaram"))
        .tag("band", band)
        .tag("pci", str(cfg.get("pci", 0)))
        .tag("du_id", DU_ID)
        .tag("cu_id", cfg.get("cu_id", "CU-MLS"))
        .tag("vendor", cfg.get("vendor", "Nokia"))
        .tag("generation", cfg.get("generation", "5G"))
        .field("connected_ues", connected)
        .field("dl_throughput_mbps", round(dl_tp, 2))
        .field("ul_throughput_mbps", round(ul_tp, 2))
        .field("rsrp_dbm", round(rsrp, 2))
        .field("rsrq_db", round(rsrq, 2))
        .field("sinr_db", round(sinr, 2))
        .field("power_w", round(power, 2))
        .field("prb_dl_pct", round(prb_dl, 2))
        .field("prb_ul_pct", round(prb_ul, 2))
        .field("packet_loss_pct", round(ploss, 3))
        .field("cqi", int(cqi))
        .field("mcs", int(mcs))
        .field("bler_pct", round(bler, 3))
        .field("latency_ms", round(latency, 2))
        .field("jitter_ms", round(jitter, 2))
        .field("interference_dbm", round(interference, 2))
        .field("ho_success_rate", round(ho_sr, 4))
    )
    return p, prb_dl


def du_kpi_point(states):
    total_ues = sum(len(s.ues) for s in states)
    total_max = sum(s.cfg["max_ues"] for s in states) or 1
    load = total_ues / total_max
    p = (
        Point("du_kpi")
        .tag("du_id", DU_ID)
        .tag("cu_id", states[0].cfg.get("cu_id", "CU-MLS") if states else "CU-MLS")
        .field("active_ues", total_ues)
        .field("cell_count", len(states))
        .field("cpu_pct", round(20 + load * 62 + _n(0, 3), 2))
        .field("memory_pct", round(30 + load * 45 + _n(0, 2), 2))
        .field("fronthaul_latency_us", round(_u(50, 200), 2))
        .field("processing_delay_ms", round(_u(0.1, 0.9), 3))
        .field("f1_msg_per_sec", int(total_ues * _u(0.5, 2.0)))
    )
    return p


def mobility_points(states):
    """Handovers within this DU; physically moves a UE into the target cell pool."""
    points = []
    if len(states) < 2:
        return points
    for src in states:
        connected = len(src.ues)
        if connected < 2:
            continue
        n_ho = int(connected * 0.015 * random.random())
        for _ in range(n_ho):
            if len(src.ues) < 2:
                break
            targets = [s for s in states if s is not src]
            if not targets:
                break
            tgt = random.choice(targets)
            ue = src.ues.pop()
            tgt.ues.append(ue)  # physical move into target pool
            points.append(
                Point("ue_mobility")
                .tag("ue_id", ue["id"])
                .tag("source_cell", src.cell_id)
                .tag("target_cell", tgt.cell_id)
                .tag("event_type", "handover")
                .field("rsrp_source", round(-70 + _n(0, 8), 2))
                .field("rsrp_target", round(-62 + _n(0, 8), 2))
                .field("ho_duration_ms", round(_u(18, 65), 2))
                .field("velocity_kmh", round(_u(0, 90), 2))
            )
    return points


def usage_points(states):
    """ue_usage samples, <=8 UEs/cell/tick."""
    points = []
    for s in states:
        if not s.ues:
            continue
        peak_dl = s.cfg["peak_dl_mbps"]
        per_ue_share = peak_dl / max(len(s.ues), 1) * 1e6 / 8  # rough bytes/tick basis
        sample = random.sample(s.ues, min(8, len(s.ues)))
        for ue in sample:
            slc = ue["slice"]
            if slc == "URLLC":
                dl = _u(1e3, 60e3); ul = _u(0.5e3, 25e3)
                lat = _u(0.5, 4); jit = _u(0.1, 0.8)
            elif slc == "mMTC":
                dl = _u(10, 2e3); ul = _u(10, 800)
                lat = _u(10, 150); jit = _u(2, 30)
            else:  # eMBB
                dl = per_ue_share * _u(0.5, 1.5); ul = dl * _u(0.08, 0.22)
                lat = _u(5, 35); jit = _u(0.5, 6)
            points.append(
                Point("ue_usage")
                .tag("ue_id", ue["id"])
                .tag("cell_id", s.cell_id)
                .tag("slice_type", slc)
                .field("dl_bytes", round(dl, 1))
                .field("ul_bytes", round(ul, 1))
                .field("latency_ms", round(lat, 2))
                .field("jitter_ms", round(jit, 2))
                .field("packet_loss", round(_u(0, 0.003), 5))
            )
    return points


def read_topology():
    try:
        with open(TOPOLOGY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[{DU_ID}] topology read failed: {e}", flush=True)
        return None


def my_cells(topo):
    du = topo.get("dus", {}).get(DU_ID)
    if not du:
        return {}
    cu_id = du.get("cu_id", "CU-MLS")
    out = {}
    for cid in du.get("cell_ids", []):
        cfg = topo.get("cells", {}).get(cid)
        if cfg:
            cfg = dict(cfg)
            cfg["cu_id"] = cu_id
            out[cid] = cfg
    return out


def main():
    print(f"[{DU_ID}] starting; influx={INFLUX_URL} interval={INTERVAL_SEC}s", flush=True)
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    states = {}
    last_topo_poll = 0.0
    cells = {}

    while True:
        now = time.time()
        if now - last_topo_poll >= TOPO_POLL_SEC:
            topo = read_topology()
            if topo:
                cells = my_cells(topo)
                # add new cells
                for cid, cfg in cells.items():
                    if cid not in states:
                        states[cid] = CellState(cid, cfg)
                    else:
                        states[cid].update_cfg(cfg)
                # drop cells no longer ours
                for cid in list(states):
                    if cid not in cells:
                        del states[cid]
            last_topo_poll = now

        active = [states[cid] for cid in cells if cid in states]
        if active:
            points = []
            for s in active:
                s.step_population()
            # mobility first (moves UEs), then KPIs reflect post-HO pools
            points.extend(mobility_points(active))
            for s in active:
                p, _ = cell_kpi_point(s)
                points.append(p)
            points.append(du_kpi_point(active))
            points.extend(usage_points(active))
            try:
                write_api.write(bucket=INFLUX_BUCKET, record=points)
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                print(f"[{DU_ID}] influx write failed: {e}", flush=True)

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
