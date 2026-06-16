"""Planning engine (spec §6.3 + F.2) — FastAPI :8081."""
import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import placement
import pci_planner
import slice_allocator
import mip_placer

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://controller:8080")

app = FastAPI(title="RAN Planning Engine")
_PLANS = {}


# --------------------------------------------------------------------------
# request models (F.2)
# --------------------------------------------------------------------------
class TrafficProfile(BaseModel):
    eMBB: float = 0.70
    URLLC: float = 0.20
    mMTC: float = 0.10
    peak_hour: int = 19


class LatencyConstraints(BaseModel):
    e2e_ms: float = 10.0
    fronthaul_us: float = 100.0


class ComputeResources(BaseModel):
    cpu_cores_per_site: int = 32
    ram_gb_per_site: int = 64


class PlanRequest(BaseModel):
    geographic_area: str = "Bangalore"
    expected_user_density: float = 500.0
    traffic_profile: TrafficProfile = Field(default_factory=TrafficProfile)
    fiber_availability: list = Field(default_factory=list)
    spectrum_bands: list = Field(default_factory=lambda: ["n78", "n28"])
    latency_constraints: LatencyConstraints = Field(default_factory=LatencyConstraints)
    compute_resources: ComputeResources = Field(default_factory=ComputeResources)
    deployment_budget: float = 2_000_000.0
    max_cells_per_du: int = 3
    max_dus_per_cu: int = 4
    use_mip: bool = False
    sinr_min_db: float = 10.0
    mip_time_limit_sec: int = 120


class TimePeriodDemand(BaseModel):
    period: int
    cluster_ids: list[str]
    description: str = ""


class MultiPeriodPlanRequest(BaseModel):
    geographic_area: str = "Bangalore"
    demand_mode: str = "permanent"   # "permanent" | "temporary"
    time_periods: list[TimePeriodDemand] = Field(default_factory=list)
    spectrum_bands: list = Field(default_factory=lambda: ["n78", "n28"])
    deployment_budget: float = 2_000_000.0
    traffic_profile: TrafficProfile = Field(default_factory=TrafficProfile)
    latency_constraints: LatencyConstraints = Field(default_factory=LatencyConstraints)
    max_cells_per_du: int = 3
    max_dus_per_cu: int = 4
    sinr_min_db: float = 10.0
    mip_time_limit_sec: int = 120


class ApplyRequest(BaseModel):
    plan_id: str


# --------------------------------------------------------------------------
# plan assembly
# --------------------------------------------------------------------------
def _assemble(area, cells, traffic_profile, latency, bands, budget,
              max_cells_per_du, max_dus_per_cu, method, mip_placement=None):
    # PCI assignment
    pci_map, violations = pci_planner.assign_pcis(cells)
    for c in cells:
        c["pci"] = pci_map.get(c["cell_id"], 0)

    # DU/CU grouping
    dus, cus = placement.group_dus_cus(cells, max_cells_per_du, max_dus_per_cu)
    du_of = {cid: d["du_id"] for d in dus for cid in d["cell_ids"]}
    cu_of_du = {d["du_id"]: d.get("cu_id") for d in dus}

    total_max = sum(c["max_ues"] for c in cells)
    slices, slice_warn = slice_allocator.allocate(
        traffic_profile, total_max, latency.get("e2e_ms", 10.0))

    for c in cells:
        c["du_id"] = du_of.get(c["cell_id"])
        c["cu_id"] = cu_of_du.get(c["du_id"])
        c["slices"] = slices
        c["slice_warnings"] = slice_warn

    timing = slice_allocator.timing_sync_strategy(bands, latency.get("fronthaul_us", 100.0))
    n_cells, n_dus, n_cus = len(cells), len(dus), len(cus)
    cost = placement.estimated_cost(n_cells, n_dus, n_cus)

    plan = {
        "plan_id": uuid.uuid4().hex[:8],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "geographic_area": area,
        "timing_sync": timing,
        "pci_violations": violations,
        "cells": cells,
        "dus": dus,
        "cus": cus,
        "summary": {
            "n_cells": n_cells, "n_dus": n_dus, "n_cus": n_cus,
            "total_capacity_ues": total_max,
            "estimated_cost_usd": cost,
            "budget_utilisation_pct": round(cost / budget * 100, 2) if budget else None,
            "placement_method": method,
        },
    }
    if mip_placement is not None:
        plan["mip_placement"] = {
            "status": mip_placement["status"],
            "install_cost": mip_placement["install_cost"],
            "op_cost": mip_placement["op_cost"],
            "total_cost": mip_placement["total_cost"],
            "build_schedule": mip_placement["build_schedule"],
            "feasibility": {k: len(v) for k, v in mip_placement["feasibility"].items()},
        }
    _PLANS[plan["plan_id"]] = plan
    return plan


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/candidates")
def candidates():
    return placement.candidate_cells()


@app.get("/demand-clusters")
def demand_clusters():
    return {
        "clusters": [
            {"cluster_id": k, "lat": v[0], "lon": v[1], "n_channels": v[2]}
            for k, v in mip_placer.BANGALORE_DEMAND_CLUSTERS.items()
        ],
        "presets": {"permanent": mip_placer.CASE_A, "temporary": mip_placer.CASE_B},
    }


@app.post("/plan")
def plan(req: PlanRequest):
    traffic = req.traffic_profile.model_dump()
    latency = req.latency_constraints.model_dump()
    if req.use_mip:
        result = mip_placer.select_cells_mip(
            req.deployment_budget, req.spectrum_bands,
            [list(mip_placer.BANGALORE_DEMAND_CLUSTERS.keys())],
            req.sinr_min_db, req.mip_time_limit_sec)
        cells = [dict(c) for c in result["selected_cells"]]
        method = "mip" if result["source"] == "mip" else "heuristic"
        return _assemble(req.geographic_area, cells, traffic, latency,
                         req.spectrum_bands, req.deployment_budget,
                         req.max_cells_per_du, req.max_dus_per_cu, method, result)
    cells = [dict(c) for c in placement.select_cells(
        req.expected_user_density, req.deployment_budget, req.spectrum_bands)]
    return _assemble(req.geographic_area, cells, traffic, latency,
                     req.spectrum_bands, req.deployment_budget,
                     req.max_cells_per_du, req.max_dus_per_cu, "heuristic")


@app.post("/plan/multi-period")
def plan_multi_period(req: MultiPeriodPlanRequest):
    if req.time_periods:
        period_clusters = [tp.cluster_ids for tp in sorted(req.time_periods, key=lambda x: x.period)]
    else:
        period_clusters = (mip_placer.CASE_A if req.demand_mode == "permanent"
                           else mip_placer.CASE_B)
    result = mip_placer.select_cells_mip(
        req.deployment_budget, req.spectrum_bands, period_clusters,
        req.sinr_min_db, req.mip_time_limit_sec)
    cells = [dict(c) for c in result["selected_cells"]]
    method = "mip" if result["source"] == "mip" else "heuristic"
    plan = _assemble(req.geographic_area, cells, req.traffic_profile.model_dump(),
                     req.latency_constraints.model_dump(), req.spectrum_bands,
                     req.deployment_budget, req.max_cells_per_du, req.max_dus_per_cu,
                     method, result)
    plan["demand_mode"] = req.demand_mode
    plan["period_clusters"] = period_clusters
    return plan


@app.get("/plan/{plan_id}")
def get_plan(plan_id: str):
    p = _PLANS.get(plan_id)
    if not p:
        raise HTTPException(status_code=404, detail="plan not found")
    return p


@app.post("/plan/apply")
def apply_plan(req: ApplyRequest):
    p = _PLANS.get(req.plan_id)
    if not p:
        raise HTTPException(status_code=404, detail="plan not found")
    payload = placement.plan_to_topology(p)
    try:
        r = httpx.post(f"{CONTROLLER_URL}/topology/replace", json=payload, timeout=30)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"controller apply failed: {e}")
    return {"status": "applied", "plan_id": req.plan_id, "controller_response": r.json()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
