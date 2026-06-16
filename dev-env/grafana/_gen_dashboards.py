"""Generate the five Grafana dashboards (spec H.6) into ./dashboards/*.json.

Run:  python _gen_dashboards.py
Each dashboard targets the provisioned InfluxDB-Telecom datasource (uid influxdb-telecom)
via Flux against the measurements in Appendix A.4.
"""
import json
import os

DS = {"type": "influxdb", "uid": "influxdb-telecom"}
BUCKET = "telecom_metrics"
HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "dashboards")


def flux_field(measurement, field, agg="mean", extra="", legend=None):
    # `legend` is accepted for call-site convenience; series naming is driven by
    # the Flux group columns in Grafana, so it is intentionally unused here.
    return (
        f'from(bucket: "{BUCKET}")\n'
        f'  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
        f'  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}")\n'
        f'{extra}'
        f'  |> aggregateWindow(every: v.windowPeriod, fn: {agg}, createEmpty: false)'
    )


def flux_sum_latest(measurement, field):
    return (
        f'from(bucket: "{BUCKET}")\n'
        f'  |> range(start: -5m)\n'
        f'  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}")\n'
        f'  |> last()\n'
        f'  |> group()\n'
        f'  |> sum()'
    )


def panel(pid, title, ptype, x, y, w, h, query, unit=None, legend="{{cell_id}}"):
    fc = {"defaults": {"custom": {}}, "overrides": []}
    if unit:
        fc["defaults"]["unit"] = unit
    p = {
        "id": pid, "title": title, "type": ptype,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": DS,
        "targets": [{"refId": "A", "datasource": DS, "query": query}],
        "fieldConfig": fc,
        "options": {},
    }
    if ptype == "timeseries":
        p["options"] = {"legend": {"displayMode": "list", "placement": "bottom"}}
        p["targets"][0]["query"] = query  # already aggregated
    return p


def dashboard(uid, title, panels):
    return {
        "uid": uid, "title": title, "schemaVersion": 39, "version": 1,
        "editable": True, "time": {"from": "now-1h", "to": "now"},
        "refresh": "30s", "tags": ["telecom"], "timezone": "browser",
        "templating": {"list": []}, "annotations": {"list": []},
        "panels": panels,
    }


def build():
    os.makedirs(OUT, exist_ok=True)

    # 1. network overview
    overview = dashboard("net-overview", "Malleswaram Network Overview", [
        panel(1, "Total Connected UEs", "stat", 0, 0, 6, 4,
              flux_sum_latest("cell_kpi", "connected_ues")),
        panel(2, "Avg SINR (dB)", "stat", 6, 0, 6, 4,
              'from(bucket: "telecom_metrics") |> range(start: -5m) '
              '|> filter(fn: (r) => r._measurement == "cell_kpi" and r._field == "sinr_db") '
              '|> mean() |> group() |> mean()', unit="none"),
        panel(3, "Total Power (kW)", "stat", 12, 0, 6, 4,
              'from(bucket: "telecom_metrics") |> range(start: -5m) '
              '|> filter(fn: (r) => r._measurement == "cell_kpi" and r._field == "power_w") '
              '|> last() |> group() |> sum() |> map(fn: (r) => ({r with _value: r._value / 1000.0}))',
              unit="kwatt"),
        panel(4, "Overloaded Cells (PRB>85%)", "stat", 18, 0, 6, 4,
              'from(bucket: "telecom_metrics") |> range(start: -5m) '
              '|> filter(fn: (r) => r._measurement == "cell_kpi" and r._field == "prb_dl_pct") '
              '|> last() |> filter(fn: (r) => r._value > 85.0) |> group() |> count()'),
        panel(5, "Connected UEs over time", "timeseries", 0, 4, 12, 8,
              flux_field("cell_kpi", "connected_ues", "mean")),
        panel(6, "Avg DL Throughput (Mbps)", "timeseries", 12, 4, 12, 8,
              flux_field("cell_kpi", "dl_throughput_mbps", "mean")),
    ])

    # 2. cell KPI timeseries
    cell = dashboard("cell-kpi", "Cell KPI Timeseries", [
        panel(1, "PRB DL %", "timeseries", 0, 0, 12, 8, flux_field("cell_kpi", "prb_dl_pct"), "percent"),
        panel(2, "SINR (dB)", "timeseries", 12, 0, 12, 8, flux_field("cell_kpi", "sinr_db")),
        panel(3, "RSRP (dBm)", "timeseries", 0, 8, 12, 8, flux_field("cell_kpi", "rsrp_dbm")),
        panel(4, "DL Throughput (Mbps)", "timeseries", 12, 8, 12, 8, flux_field("cell_kpi", "dl_throughput_mbps")),
        panel(5, "Power (W)", "timeseries", 0, 16, 8, 8, flux_field("cell_kpi", "power_w"), "watt"),
        panel(6, "CQI", "timeseries", 8, 16, 8, 8, flux_field("cell_kpi", "cqi")),
        panel(7, "BLER % / Latency (ms)", "timeseries", 16, 16, 8, 8, flux_field("cell_kpi", "bler_pct")),
    ])

    # 3. DU/CU performance
    ducu = dashboard("du-cu-perf", "DU / CU Performance", [
        panel(1, "DU CPU %", "timeseries", 0, 0, 12, 8, flux_field("du_kpi", "cpu_pct", legend="{{du_id}}"), "percent"),
        panel(2, "DU Memory %", "timeseries", 12, 0, 12, 8, flux_field("du_kpi", "memory_pct"), "percent"),
        panel(3, "Fronthaul Latency (us)", "timeseries", 0, 8, 8, 8, flux_field("du_kpi", "fronthaul_latency_us")),
        panel(4, "F1 msg/s", "timeseries", 8, 8, 8, 8, flux_field("du_kpi", "f1_msg_per_sec")),
        panel(5, "DU Active UEs", "timeseries", 16, 8, 8, 8, flux_field("du_kpi", "active_ues")),
        panel(6, "CU PDCP DL (Gbps)", "timeseries", 0, 16, 8, 8, flux_field("cu_kpi", "pdcp_dl_gbps", legend="{{cu_id}}")),
        panel(7, "AMF Registered UEs", "timeseries", 8, 16, 8, 8, flux_field("core_kpi", "registered_ues", legend="{{component}}")),
        panel(8, "UPF DL Throughput (Gbps)", "timeseries", 16, 16, 8, 8, flux_field("core_kpi", "dl_throughput_gbps")),
    ])

    # 4. SON alerts & actions
    son = dashboard("son-alerts", "SON Alerts & Autonomous Actions", [
        panel(1, "CRITICAL alerts (1h)", "stat", 0, 0, 6, 4,
              'from(bucket: "telecom_metrics") |> range(start: -1h) '
              '|> filter(fn: (r) => r._measurement == "alerts" and r.severity == "CRITICAL") '
              '|> group() |> count(column: "_time")'),
        panel(2, "WARNING alerts (1h)", "stat", 6, 0, 6, 4,
              'from(bucket: "telecom_metrics") |> range(start: -1h) '
              '|> filter(fn: (r) => r._measurement == "alerts" and r.severity == "WARNING") '
              '|> group() |> count(column: "_time")'),
        panel(3, "SON actions (1h)", "stat", 12, 0, 6, 4,
              'from(bucket: "telecom_metrics") |> range(start: -1h) '
              '|> filter(fn: (r) => r._measurement == "son_actions" and r._field == "confidence") '
              '|> group() |> count()'),
        panel(4, "AI Confidence over time", "timeseries", 18, 0, 6, 4,
              flux_field("alerts", "ai_confidence", legend="{{alert_type}}")),
        panel(5, "Alert metric values", "timeseries", 0, 4, 12, 8,
              flux_field("alerts", "metric_value", legend="{{alert_type}}")),
        panel(6, "SON action confidence", "timeseries", 12, 4, 12, 8,
              flux_field("son_actions", "confidence", legend="{{action_type}}")),
    ])

    # 5. UE analytics
    ue = dashboard("ue-analytics", "UE Analytics", [
        panel(1, "DL bytes by slice", "timeseries", 0, 0, 12, 8,
              flux_field("ue_usage", "dl_bytes", legend="{{slice_type}}")),
        panel(2, "Latency by slice (ms)", "timeseries", 12, 0, 12, 8,
              flux_field("ue_usage", "latency_ms", legend="{{slice_type}}")),
        panel(3, "Jitter by slice (ms)", "timeseries", 0, 8, 12, 8,
              flux_field("ue_usage", "jitter_ms", legend="{{slice_type}}")),
        panel(4, "Handover duration (ms)", "timeseries", 12, 8, 12, 8,
              flux_field("ue_mobility", "ho_duration_ms", legend="{{event_type}}")),
    ])

    files = {
        "network_overview.json": overview,
        "cell_kpi.json": cell,
        "du_cu_performance.json": ducu,
        "son_alerts.json": son,
        "ue_analytics.json": ue,
    }
    for name, db in files.items():
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2)
            f.write("\n")
    print(f"OK: wrote {len(files)} dashboards to {OUT}")
    for name in files:
        print(f"  - {name}")


if __name__ == "__main__":
    build()
