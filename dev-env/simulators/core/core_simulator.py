"""Core simulator (spec Appendix C.3).

Runs independently of topology. Emits three core_kpi points per tick (AMF/SMF/UPF)
with exponential-smoothing state. Uses the same diurnal curve as the CU (C.2).
"""
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

MAX_UES_TOTAL = 1_625_000   # ~50% of Bangalore 13M x 25% operator share
IP_POOL_SIZE = 65536        # /16

# same curve as CU (C.2)
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


def main():
    print(f"[core] starting; influx={INFLUX_URL} interval={INTERVAL_SEC}s", flush=True)
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    lf = load_factor()
    registered = int(MAX_UES_TOTAL * lf * 1.08)
    active = int(registered * 0.90)

    while True:
        lf = load_factor()
        target_reg = int(MAX_UES_TOTAL * lf * _u(1.04, 1.12))
        registered = int(registered * 0.88 + target_reg * 0.12)
        active = int(registered * _u(0.84, 0.96))
        dl_gbps = active * _u(1.5e-5, 7e-5)
        ul_gbps = dl_gbps * _u(0.08, 0.16)

        amf = (
            Point("core_kpi")
            .tag("component", "AMF")
            .tag("instance_id", "AMF-BLR-01")
            .field("registered_ues", registered)
            .field("active_sessions", active)
            .field("nas_msg_per_sec", int(registered * _u(0.4, 2.2)))
            .field("paging_per_sec", int(_u(5, 80)))
            .field("handover_per_sec", int(_u(2, 30)))
            .field("cpu_pct", round(20 + lf * 55 + _n(0, 3), 2))
            .field("memory_pct", round(28 + lf * 42 + _n(0, 2), 2))
            .field("n2_latency_ms", round(_u(1, 9), 3))
        )
        smf = (
            Point("core_kpi")
            .tag("component", "SMF")
            .tag("instance_id", "SMF-BLR-01")
            .field("active_pdu_sessions", active)
            .field("session_setup_rate", int(_u(4, 35)))
            .field("session_release_rate", int(_u(2, 25)))
            .field("ip_pool_utilization_pct", round(active / IP_POOL_SIZE * 100, 2))
            .field("cpu_pct", round(15 + lf * 52 + _n(0, 4), 2))
            .field("memory_pct", round(25 + lf * 38 + _n(0, 2), 2))
            .field("n4_latency_ms", round(_u(0.5, 6), 3))
        )
        upf = (
            Point("core_kpi")
            .tag("component", "UPF")
            .tag("instance_id", "UPF-BLR-01")
            .field("dl_throughput_gbps", round(dl_gbps, 4))
            .field("ul_throughput_gbps", round(ul_gbps, 4))
            .field("active_tunnels", active)
            .field("packet_drop_rate", round(_u(0, 0.0015), 6))
            .field("gtp_encap_errors", random.randint(0, 5))
            .field("cpu_pct", round(25 + lf * 60 + _n(0, 4), 2))
            .field("memory_pct", round(35 + lf * 35 + _n(0, 2), 2))
        )
        try:
            write_api.write(bucket=INFLUX_BUCKET, record=[amf, smf, upf])
        except Exception as e:  # noqa: BLE001
            print(f"[core] influx write failed: {e}", flush=True)
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
