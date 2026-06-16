"""Controller (spec §6.2 + F.1) — single control plane / source of truth.

Owns topology.json (only writer; atomic .tmp -> rename), merges live KPIs from
InfluxDB, and exposes move / add / remove / replace / pci-reopt mutations. Every
mutation bumps `version` and writes a `topology_event`.
"""
import json
import math
import os
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "telecom")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "telecom_metrics")
TOPOLOGY_FILE = os.getenv("TOPOLOGY_FILE", "/config/topology.json")

EARTH_R_KM = 6371.0
PCI_MAX = 1007
PCI_REOPT_RADIUS_KM = 3.0

app = FastAPI(title="RAN Controller")
_lock = threading.RLock()

_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
_write_api = _client.write_api(write_options=SYNCHRONOUS)
_query_api = _client.query_api()


# --------------------------------------------------------------------------
# topology persistence
# --------------------------------------------------------------------------
def load_topo():
    with open(TOPOLOGY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_topo(topo, updated_by):
    topo["version"] = int(topo.get("version", 0)) + 1
    topo["last_updated"] = datetime.now(timezone.utc).isoformat()
    topo["updated_by"] = updated_by
    tmp = TOPOLOGY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(topo, f, indent=2)
        f.write("\n")
    os.replace(tmp, TOPOLOGY_FILE)
    return topo


def write_event(event_type, **fields):
    p = Point("topology_event").tag("event_type", event_type)
    for k, v in fields.items():
        if v is not None:
            p = p.field(k, str(v))
    try:
        _write_api.write(bucket=INFLUX_BUCKET, record=p)
    except Exception as e:  # noqa: BLE001
        print(f"[controller] event write failed: {e}", flush=True)


def cell_du_cu(topo, cell_id):
    """Resolve a cell's live (du_id, cu_id) from topology."""
    for did, du in topo.get("dus", {}).items():
        if cell_id in du.get("cell_ids", []):
            return did, du.get("cu_id")
    return None, None


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------
# InfluxDB latest-KPI helpers
# --------------------------------------------------------------------------
def _query_latest(measurement, key_tags, start="-5m"):
    flux = (
        f'from(bucket: "{INFLUX_BUCKET}") '
        f'|> range(start: {start}) '
        f'|> filter(fn: (r) => r._measurement == "{measurement}") '
        f'|> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")'
    )
    latest = {}
    try:
        tables = _query_api.query(flux)
    except Exception as e:  # noqa: BLE001
        print(f"[controller] query {measurement} failed: {e}", flush=True)
        return latest
    for table in tables:
        for rec in table.records:
            vals = rec.values
            key = tuple(vals.get(t) for t in key_tags)
            t = rec.get_time()
            if key not in latest or t > latest[key][0]:
                latest[key] = (t, vals)
    return latest


_DROP = {"result", "table", "_start", "_stop", "_time", "_measurement"}


def _clean(vals, drop_tags):
    return {k: v for k, v in vals.items() if k not in _DROP and k not in drop_tags}


def latest_cell_kpis():
    raw = _query_latest("cell_kpi", ["cell_id"])
    tagset = {"cell_id", "area", "band", "pci", "du_id", "cu_id", "vendor", "generation"}
    return {k[0]: _clean(v[1], tagset) for k, v in raw.items() if k[0]}


def latest_du_kpis():
    raw = _query_latest("du_kpi", ["du_id"])
    return {k[0]: _clean(v[1], {"du_id", "cu_id"}) for k, v in raw.items() if k[0]}


def latest_cu_kpis():
    raw = _query_latest("cu_kpi", ["cu_id"])
    return {k[0]: _clean(v[1], {"cu_id"}) for k, v in raw.items() if k[0]}


def latest_core_kpis():
    raw = _query_latest("core_kpi", ["component"])
    return {k[0]: _clean(v[1], {"component", "instance_id"}) for k, v in raw.items() if k[0]}


def cell_series(cell_id, start="-30m"):
    flux = (
        f'from(bucket: "{INFLUX_BUCKET}") '
        f'|> range(start: {start}) '
        f'|> filter(fn: (r) => r._measurement == "cell_kpi" and r.cell_id == "{cell_id}") '
        f'|> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value") '
        f'|> sort(columns:["_time"])'
    )
    out = []
    try:
        for table in _query_api.query(flux):
            for rec in table.records:
                row = {"_time": rec.get_time().isoformat()}
                row.update(_clean(rec.values, {"cell_id", "area", "band", "pci",
                                               "du_id", "cu_id", "vendor", "generation"}))
                out.append(row)
    except Exception as e:  # noqa: BLE001
        print(f"[controller] series query failed: {e}", flush=True)
    return out


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------
class MoveCellRequest(BaseModel):
    cell_id: str
    to_du_id: str


class MoveDuRequest(BaseModel):
    du_id: str
    to_cu_id: str


class AddCellRequest(BaseModel):
    cell_id: str
    du_id: str
    area: str
    lat: float
    lon: float
    generation: str = "5G"
    band: str = "n78"
    freq_mhz: int = 3500
    pci: int = 0
    vendor: str = "Nokia"
    hardware_model: str = "AirScale MAA 64T64R"
    antenna_config: str = "64T64R"
    peak_dl_mbps: int = 3800
    tx_power_w: int = 1000
    idle_power_w: int = 250
    max_ues: int = 900


class ReplaceRequest(BaseModel):
    cus: dict
    dus: dict
    cells: dict


class PciReoptRequest(BaseModel):
    cell_id: str


# --------------------------------------------------------------------------
# read endpoints
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/topology")
def get_topology():
    with _lock:
        return load_topo()


@app.get("/network")
def get_network():
    with _lock:
        topo = load_topo()
    ck = latest_cell_kpis()
    dk = latest_du_kpis()
    cuk = latest_cu_kpis()
    core = latest_core_kpis()

    cells = {}
    for cid, cfg in topo.get("cells", {}).items():
        did, cuid = cell_du_cu(topo, cid)
        cells[cid] = {**cfg, "du_id": did, "cu_id": cuid, "kpi": ck.get(cid, {})}
    dus = {}
    for did, du in topo.get("dus", {}).items():
        dus[did] = {**du, "kpi": dk.get(did, {})}
    cus = {}
    for cuid, cu in topo.get("cus", {}).items():
        cus[cuid] = {**cu, "kpi": cuk.get(cuid, {})}
    return {
        "cells": cells,
        "dus": dus,
        "cus": cus,
        "core": core,
        "topology_version": topo.get("version"),
        "last_updated": topo.get("last_updated"),
    }


@app.get("/cells")
def get_cells(area: str | None = None, du_id: str | None = None, cu_id: str | None = None):
    with _lock:
        topo = load_topo()
    ck = latest_cell_kpis()
    out = []
    for cid, cfg in topo.get("cells", {}).items():
        did, cuid = cell_du_cu(topo, cid)
        if area and cfg.get("area") != area:
            continue
        if du_id and did != du_id:
            continue
        if cu_id and cuid != cu_id:
            continue
        out.append({**cfg, "cell_id": cid, "du_id": did, "cu_id": cuid, "kpi": ck.get(cid, {})})
    return out


@app.get("/cells/{cell_id}")
def get_cell(cell_id: str):
    with _lock:
        topo = load_topo()
    cfg = topo.get("cells", {}).get(cell_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="cell not found")
    did, cuid = cell_du_cu(topo, cell_id)
    return {**cfg, "cell_id": cell_id, "du_id": did, "cu_id": cuid,
            "series": cell_series(cell_id)}


@app.get("/dus")
def get_dus():
    with _lock:
        topo = load_topo()
    dk = latest_du_kpis()
    return [{**du, "du_id": did, "kpi": dk.get(did, {})}
            for did, du in topo.get("dus", {}).items()]


@app.get("/cus")
def get_cus():
    with _lock:
        topo = load_topo()
    cuk = latest_cu_kpis()
    return [{**cu, "cu_id": cuid, "kpi": cuk.get(cuid, {})}
            for cuid, cu in topo.get("cus", {}).items()]


@app.get("/neighbors/{cell_id}")
def neighbors(cell_id: str, max_neighbors: int = Query(6, ge=1, le=30)):
    with _lock:
        topo = load_topo()
    me = topo.get("cells", {}).get(cell_id)
    if not me:
        raise HTTPException(status_code=404, detail="cell not found")
    dists = []
    for cid, cfg in topo.get("cells", {}).items():
        if cid == cell_id:
            continue
        d = haversine_km(me["lat"], me["lon"], cfg["lat"], cfg["lon"])
        dists.append((d, cid, cfg))
    dists.sort(key=lambda x: x[0])
    return {
        "cell_id": cell_id,
        "neighbors": [{"cell_id": cid, "distance_km": round(d, 4), **cfg}
                      for d, cid, cfg in dists[:max_neighbors]],
    }


# --------------------------------------------------------------------------
# mutations
# --------------------------------------------------------------------------
@app.post("/move/cell")
def move_cell(req: MoveCellRequest):
    with _lock:
        topo = load_topo()
        if req.cell_id not in topo.get("cells", {}):
            raise HTTPException(status_code=404, detail="cell not found")
        if req.to_du_id not in topo.get("dus", {}):
            raise HTTPException(status_code=404, detail="target DU not found")
        from_du, _ = cell_du_cu(topo, req.cell_id)
        for du in topo["dus"].values():
            if req.cell_id in du.get("cell_ids", []):
                du["cell_ids"].remove(req.cell_id)
        topo["dus"][req.to_du_id].setdefault("cell_ids", []).append(req.cell_id)
        save_topo(topo, f"move_cell:{req.cell_id}")
    write_event("cell_move", cell_id=req.cell_id, **{"from": from_du, "to": req.to_du_id})
    return {"status": "ok", "cell_id": req.cell_id, "from_du": from_du, "to_du": req.to_du_id}


@app.post("/move/du")
def move_du(req: MoveDuRequest):
    with _lock:
        topo = load_topo()
        if req.du_id not in topo.get("dus", {}):
            raise HTTPException(status_code=404, detail="DU not found")
        if req.to_cu_id not in topo.get("cus", {}):
            raise HTTPException(status_code=404, detail="target CU not found")
        from_cu = topo["dus"][req.du_id].get("cu_id")
        for cu in topo["cus"].values():
            if req.du_id in cu.get("du_ids", []):
                cu["du_ids"].remove(req.du_id)
        topo["cus"][req.to_cu_id].setdefault("du_ids", []).append(req.du_id)
        topo["dus"][req.du_id]["cu_id"] = req.to_cu_id
        save_topo(topo, f"move_du:{req.du_id}")
    write_event("du_move", du_id=req.du_id, **{"from": from_cu, "to": req.to_cu_id})
    return {"status": "ok", "du_id": req.du_id, "from_cu": from_cu, "to_cu": req.to_cu_id}


@app.post("/topology/replace")
def topology_replace(req: ReplaceRequest):
    with _lock:
        topo = load_topo()
        topo["cus"] = req.cus
        topo["dus"] = req.dus
        topo["cells"] = req.cells
        save_topo(topo, "topology_replace")
    write_event("topology_replace", cell_id=f"{len(req.cells)} cells")
    return {"status": "ok", "n_cells": len(req.cells), "n_dus": len(req.dus), "n_cus": len(req.cus)}


@app.post("/cells/add")
def add_cell(req: AddCellRequest):
    with _lock:
        topo = load_topo()
        if req.cell_id in topo.get("cells", {}):
            raise HTTPException(status_code=409, detail="cell already exists")
        if req.du_id not in topo.get("dus", {}):
            raise HTTPException(status_code=404, detail="DU not found")
        pci = req.pci
        if pci == 0:
            used = {c.get("pci") for c in topo["cells"].values()}
            pci = next((p for p in range(1, 1024) if p not in used), 1)
        cfg = req.model_dump(exclude={"du_id"})
        cfg["pci"] = pci
        cell_id = cfg.pop("cell_id")
        topo["cells"][cell_id] = cfg
        topo["dus"][req.du_id].setdefault("cell_ids", []).append(cell_id)
        save_topo(topo, f"add_cell:{cell_id}")
    write_event("cell_add", cell_id=req.cell_id, to=req.du_id)
    return {"status": "ok", "cell_id": req.cell_id, "du_id": req.du_id, "pci": pci}


@app.delete("/cells/{cell_id}")
def remove_cell(cell_id: str):
    with _lock:
        topo = load_topo()
        if cell_id not in topo.get("cells", {}):
            raise HTTPException(status_code=404, detail="cell not found")
        from_du, _ = cell_du_cu(topo, cell_id)
        del topo["cells"][cell_id]
        for du in topo["dus"].values():
            if cell_id in du.get("cell_ids", []):
                du["cell_ids"].remove(cell_id)
        save_topo(topo, f"remove_cell:{cell_id}")
    write_event("cell_remove", cell_id=cell_id, **{"from": from_du})
    return {"status": "ok", "cell_id": cell_id, "from_du": from_du}


@app.post("/son/pci-reopt")
def pci_reopt(req: PciReoptRequest):
    with _lock:
        topo = load_topo()
        me = topo.get("cells", {}).get(req.cell_id)
        if not me:
            raise HTTPException(status_code=404, detail="cell not found")
        old_pci = me.get("pci", 0)
        # neighbours within 3 km
        nb_pcis, nb_mod3 = set(), set()
        for cid, cfg in topo["cells"].items():
            if cid == req.cell_id:
                continue
            if haversine_km(me["lat"], me["lon"], cfg["lat"], cfg["lon"]) <= PCI_REOPT_RADIUS_KM:
                nb_pcis.add(cfg.get("pci"))
                nb_mod3.add(cfg.get("pci", 0) % 3)
        new_pci = None
        for p in range(0, PCI_MAX + 1):
            if p not in nb_pcis and (p % 3) not in nb_mod3:
                new_pci = p
                break
        if new_pci is None:  # confusion unavoidable -> smallest collision-free
            for p in range(0, PCI_MAX + 1):
                if p not in nb_pcis:
                    new_pci = p
                    break
        changed = new_pci is not None and new_pci != old_pci
        if changed:
            me["pci"] = new_pci
            save_topo(topo, f"pci_reopt:{req.cell_id}")
    if changed:
        write_event("pci_reopt", cell_id=req.cell_id, **{"from": old_pci, "to": new_pci})
    return {"status": "ok", "cell_id": req.cell_id, "old_pci": old_pci,
            "new_pci": new_pci if new_pci is not None else old_pci, "changed": bool(changed)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
