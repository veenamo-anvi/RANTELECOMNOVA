"""Orchestrator tools (spec §6.1).

13 tools stored as Anthropic-style schemas; `_clean_params()` translates them to
Gemini `function_declarations` (strips `default`, removes empty `enum` arrays,
deep-copies). `TOOL_MAP` holds the synchronous implementations.
"""
import copy
import os

import httpx
from influxdb_client import InfluxDBClient

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://controller:8080")
PLANNING_URL = os.getenv("PLANNING_URL", "http://planning-api:8081")
INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "telecom")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "telecom_metrics")

_influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)


# --------------------------------------------------------------------------
# tool schemas (Anthropic-style)
# --------------------------------------------------------------------------
TOOL_SCHEMAS = [
    {
        "name": "query_network",
        "description": "Full topology + live KPIs for all 30 cells, DUs, CUs and core.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_cells",
        "description": "Filtered cell list with KPIs. Filter by area, du_id, or cu_id.",
        "input_schema": {"type": "object", "properties": {
            "area": {"type": "string"},
            "du_id": {"type": "string"},
            "cu_id": {"type": "string"},
        }},
    },
    {
        "name": "query_cell",
        "description": "Single cell config + 30-min KPI time series.",
        "input_schema": {"type": "object", "properties": {
            "cell_id": {"type": "string"}}, "required": ["cell_id"]},
    },
    {
        "name": "move_cell",
        "description": "Reassign a cell to a different DU.",
        "input_schema": {"type": "object", "properties": {
            "cell_id": {"type": "string"}, "to_du_id": {"type": "string"}},
            "required": ["cell_id", "to_du_id"]},
    },
    {
        "name": "move_du",
        "description": "Reassign a DU to a different CU.",
        "input_schema": {"type": "object", "properties": {
            "du_id": {"type": "string"}, "to_cu_id": {"type": "string"}},
            "required": ["du_id", "to_cu_id"]},
    },
    {
        "name": "plan_network",
        "description": "Generate a network plan. use_mip=true for MIP-optimal placement.",
        "input_schema": {"type": "object", "properties": {
            "geographic_area": {"type": "string", "default": "Bangalore"},
            "expected_user_density": {"type": "number", "default": 500.0},
            "spectrum_bands": {"type": "array", "items": {"type": "string"}},
            "deployment_budget": {"type": "number", "default": 2000000.0},
            "use_mip": {"type": "boolean", "default": False},
        }},
    },
    {
        "name": "plan_network_multi_period",
        "description": "Multi-period MIP plan. demand_mode permanent (Case A) or temporary (Case B).",
        "input_schema": {"type": "object", "properties": {
            "demand_mode": {"type": "string", "enum": ["permanent", "temporary"],
                            "default": "permanent"},
            "spectrum_bands": {"type": "array", "items": {"type": "string"}},
            "deployment_budget": {"type": "number", "default": 2000000.0},
        }},
    },
    {
        "name": "apply_plan",
        "description": "Push an accepted plan to the Controller as live topology.",
        "input_schema": {"type": "object", "properties": {
            "plan_id": {"type": "string"}}, "required": ["plan_id"]},
    },
    {
        "name": "get_alerts",
        "description": "Recent KPI anomaly alerts, tagged by severity and type.",
        "input_schema": {"type": "object", "properties": {
            "minutes": {"type": "integer", "default": 60},
            "severity": {"type": "string", "enum": ["", "INFO", "WARNING", "CRITICAL"]},
        }},
    },
    {
        "name": "query_ue",
        "description": "UE-level usage and mobility data. Filter by ue_id or cell_id.",
        "input_schema": {"type": "object", "properties": {
            "ue_id": {"type": "string"},
            "cell_id": {"type": "string"},
            "minutes": {"type": "integer", "default": 30},
        }},
    },
    {
        "name": "get_son_status",
        "description": "SON action summary + counts by type, last 10 actions, active alert severity.",
        "input_schema": {"type": "object", "properties": {
            "minutes": {"type": "integer", "default": 60}}},
    },
    {
        "name": "add_cell",
        "description": "Deploy a new cell; auto-assigns PCI if not provided.",
        "input_schema": {"type": "object", "properties": {
            "cell_id": {"type": "string"}, "du_id": {"type": "string"},
            "area": {"type": "string", "default": "Malleswaram"},
            "lat": {"type": "number"}, "lon": {"type": "number"},
            "generation": {"type": "string", "default": "5G"},
            "band": {"type": "string", "default": "n78"},
            "vendor": {"type": "string", "default": "Nokia"},
        }, "required": ["cell_id", "du_id", "lat", "lon"]},
    },
    {
        "name": "remove_cell",
        "description": "Decommission a cell and remove it from its DU assignment.",
        "input_schema": {"type": "object", "properties": {
            "cell_id": {"type": "string"}}, "required": ["cell_id"]},
    },
]


def _clean_params():
    """Translate Anthropic schemas to Gemini function_declarations."""
    decls = []
    for t in copy.deepcopy(TOOL_SCHEMAS):
        params = t["input_schema"]
        for prop in params.get("properties", {}).values():
            prop.pop("default", None)               # Gemini rejects `default`
            if "enum" in prop and not prop["enum"]:  # empty enum sentinel
                prop.pop("enum")
        decls.append({"name": t["name"], "description": t["description"], "parameters": params})
    return decls


GEMINI_TOOLS = [{"function_declarations": _clean_params()}]


# --------------------------------------------------------------------------
# implementations
# --------------------------------------------------------------------------
def _get(url, **params):
    p = {k: v for k, v in params.items() if v is not None}
    r = httpx.get(url, params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(url, body):
    r = httpx.post(url, json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def _flux(query):
    rows = []
    for table in _influx.query_api().query(query):
        for rec in table.records:
            rows.append({k: v for k, v in rec.values.items()
                         if k not in ("result", "table", "_start", "_stop", "_measurement")})
    return rows


def query_network(a):
    return _get(f"{CONTROLLER_URL}/network")


def list_cells(a):
    return _get(f"{CONTROLLER_URL}/cells", area=a.get("area"),
                du_id=a.get("du_id"), cu_id=a.get("cu_id"))


def query_cell(a):
    return _get(f"{CONTROLLER_URL}/cells/{a['cell_id']}")


def move_cell(a):
    return _post(f"{CONTROLLER_URL}/move/cell",
                 {"cell_id": a["cell_id"], "to_du_id": a["to_du_id"]})


def move_du(a):
    return _post(f"{CONTROLLER_URL}/move/du",
                 {"du_id": a["du_id"], "to_cu_id": a["to_cu_id"]})


def plan_network(a):
    body = {k: a[k] for k in ("geographic_area", "expected_user_density",
                              "spectrum_bands", "deployment_budget", "use_mip") if k in a}
    return _post(f"{PLANNING_URL}/plan", body)


def plan_network_multi_period(a):
    body = {k: a[k] for k in ("demand_mode", "spectrum_bands", "deployment_budget") if k in a}
    return _post(f"{PLANNING_URL}/plan/multi-period", body)


def apply_plan(a):
    return _post(f"{PLANNING_URL}/plan/apply", {"plan_id": a["plan_id"]})


def get_alerts(a):
    minutes = int(a.get("minutes", 60))
    sev = a.get("severity")
    filt = f' and r.severity == "{sev}"' if sev else ""
    q = (f'from(bucket: "{INFLUX_BUCKET}") |> range(start: -{minutes}m) '
         f'|> filter(fn: (r) => r._measurement == "alerts"{filt}) '
         f'|> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value") '
         f'|> sort(columns:["_time"], desc: true) |> limit(n: 50)')
    return _flux(q)


def query_ue(a):
    minutes = int(a.get("minutes", 30))
    conds = []
    if a.get("ue_id"):
        conds.append(f'r.ue_id == "{a["ue_id"]}"')
    if a.get("cell_id"):
        conds.append(f'r.cell_id == "{a["cell_id"]}"')
    extra = (" and " + " and ".join(conds)) if conds else ""
    q = (f'from(bucket: "{INFLUX_BUCKET}") |> range(start: -{minutes}m) '
         f'|> filter(fn: (r) => r._measurement == "ue_usage"{extra}) '
         f'|> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value") '
         f'|> sort(columns:["_time"], desc: true) |> limit(n: 50)')
    return _flux(q)


def get_son_status(a):
    minutes = int(a.get("minutes", 60))
    actions = _flux(
        f'from(bucket: "{INFLUX_BUCKET}") |> range(start: -{minutes}m) '
        f'|> filter(fn: (r) => r._measurement == "son_actions") '
        f'|> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value") '
        f'|> sort(columns:["_time"], desc: true)')
    counts = {}
    for row in actions:
        at = row.get("action_type", "?")
        counts[at] = counts.get(at, 0) + 1
    alerts = _flux(
        f'from(bucket: "{INFLUX_BUCKET}") |> range(start: -{minutes}m) '
        f'|> filter(fn: (r) => r._measurement == "alerts") '
        f'|> group(columns:["severity"]) |> count()')
    sev_counts = {row.get("severity", "?"): row.get("_value", 0) for row in alerts}
    return {"action_counts": counts, "last_actions": actions[:10],
            "alert_severity_counts": sev_counts}


def add_cell(a):
    body = {"cell_id": a["cell_id"], "du_id": a["du_id"],
            "area": a.get("area", "Malleswaram"), "lat": a["lat"], "lon": a["lon"],
            "generation": a.get("generation", "5G"), "band": a.get("band", "n78"),
            "vendor": a.get("vendor", "Nokia")}
    return _post(f"{CONTROLLER_URL}/cells/add", body)


def remove_cell(a):
    r = httpx.delete(f"{CONTROLLER_URL}/cells/{a['cell_id']}", timeout=30)
    r.raise_for_status()
    return r.json()


TOOL_MAP = {
    "query_network": query_network,
    "list_cells": list_cells,
    "query_cell": query_cell,
    "move_cell": move_cell,
    "move_du": move_du,
    "plan_network": plan_network,
    "plan_network_multi_period": plan_network_multi_period,
    "apply_plan": apply_plan,
    "get_alerts": get_alerts,
    "query_ue": query_ue,
    "get_son_status": get_son_status,
    "add_cell": add_cell,
    "remove_cell": remove_cell,
}
