"""CU simulator (spec Appendix C.2).

Uses its OWN diurnal curve (distinct from the DU curve) and applies NO weekend
factor. Domain = every cell under the DUs of CU_ID; recomputed on topology change.
"""
import json
import os
import random
import time

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "telecom")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "telecom_metrics")
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "10"))
TOPO_POLL_SEC = int(os.getenv("TOPO_POLL_SEC", "5"))
TOPOLOGY_FILE = os.getenv("TOPOLOGY_FILE", "/config/topology.json")
CU_ID = os.getenv("CU_ID", "CU-MLS")

# CU/Core diurnal curve (C.2) — distinct from DU; no weekend factor
HOURLY_LOAD = [
    0.08, 0.06, 0.05, 0.05, 0.06, 0.12, 0.30, 0.65, 0.85, 0.80, 0.70, 0.65,
    0.65, 0.60, 0.62, 0.68, 0.78, 0.90, 0.95, 1.00, 0.97, 0.88, 0.62, 0.30,
]


def load_factor():
    return HOURLY_LOAD[time.localtime().tm_hour]


def _u(a, b):
    return random.uniform(a, b)


def _n(mu, sigma):
    return random.gauss(mu, sigma)


def read_topology():
    try:
        with open(TOPOLOGY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[{CU_ID}] topology read failed: {e}", flush=True)
        return None


def domain(topo):
    """Return (du_ids, total_max_ues) for this CU."""
    cu = topo.get("cus", {}).get(CU_ID, {})
    du_ids = cu.get("du_ids", [])
    total_max = 0
    for did in du_ids:
        du = topo.get("dus", {}).get(did, {})
        for cid in du.get("cell_ids", []):
            cfg = topo.get("cells", {}).get(cid)
            if cfg:
                total_max += cfg.get("max_ues", 0)
    return du_ids, total_max


def cu_point(du_ids, total_max):
    du_count = len(du_ids)
    lf = load_factor()
    total_max = total_max or 1
    est_ues = int(total_max * lf * _u(0.88, 1.05))
    load = est_ues / total_max
    pdcp_dl = est_ues * _u(1.2e-5, 6e-5)
    pdcp_ul = pdcp_dl * _u(0.08, 0.18)
    return (
        Point("cu_kpi")
        .tag("cu_id", CU_ID)
        .field("du_count", du_count)
        .field("rrc_connected", est_ues)
        .field("rrc_idle", int(est_ues * _u(0.05, 0.20)))
        .field("rrc_setup_rate", int(_u(5, 40) * du_count))
        .field("inter_du_ho_rate", int(_u(1, 10) * du_count))
        .field("pdcp_dl_gbps", round(pdcp_dl, 5))
        .field("pdcp_ul_gbps", round(pdcp_ul, 5))
        .field("f1_latency_ms", round(_u(0.3, 2.5), 3))
        .field("n2_latency_ms", round(_u(1.0, 8.0), 3))
        .field("n3_latency_ms", round(_u(0.5, 4.0), 3))
        .field("e1_latency_ms", round(_u(0.1, 1.0), 3))
        .field("cpu_pct", round(15 + load * 55 + _n(0, 4), 2))
        .field("memory_pct", round(25 + load * 40 + _n(0, 2), 2))
    )


def main():
    print(f"[{CU_ID}] starting; influx={INFLUX_URL} interval={INTERVAL_SEC}s", flush=True)
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    du_ids, total_max = [], 0
    last_poll = 0.0
    while True:
        now = time.time()
        if now - last_poll >= TOPO_POLL_SEC:
            topo = read_topology()
            if topo:
                du_ids, total_max = domain(topo)
            last_poll = now
        try:
            write_api.write(bucket=INFLUX_BUCKET, record=cu_point(du_ids, total_max))
        except Exception as e:  # noqa: BLE001
            print(f"[{CU_ID}] influx write failed: {e}", flush=True)
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
