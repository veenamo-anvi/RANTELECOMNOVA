"""Heuristic placement & DU/CU grouping (spec Appendix E.3)."""
import math

EARTH_R_KM = 6371.0

COST_PER_CELL_USD = 50_000
COST_PER_DU_USD = 30_000
COST_PER_CU_USD = 80_000
COST_PER_SITE = COST_PER_CELL_USD + COST_PER_DU_USD  # 80,000

FRONTHAUL_RADIUS_KM = 5.0
MIDHAUL_RADIUS_KM = 25.0
DEFAULT_MAX_CELLS_PER_DU = 3
DEFAULT_MAX_DUS_PER_CU = 4

_VENDOR_CYCLE = ["Nokia", "Ericsson", "Samsung", "ZTE",
                 "Nokia", "Ericsson", "Samsung", "ZTE", "Nokia", "Ericsson"]
# vendor -> (tx_w, idle_w, peak_dl)  (E.3 note: Ericsson idle 240 here)
_HW_5G = {
    "Nokia": (1000, 250, 3800),
    "Ericsson": (950, 240, 3600),
    "Samsung": (900, 225, 3400),
    "ZTE": (1000, 250, 3200),
}
_HW_MODEL_5G = {
    "Nokia": "AirScale MAA 64T64R",
    "Ericsson": "AIR 6449",
    "Samsung": "TM500 64T64R",
    "ZTE": "AAU 5614",
}

# (cell_id, lat, lon, density_weight) — vendor assigned by index via _VENDOR_CYCLE
_CANDIDATES = [
    ("MLS_RWS_01", 13.0080, 77.5760, 1.5),
    ("MLS_18C_01", 13.0030, 77.5670, 1.4),
    ("MLS_SPG_01", 12.9990, 77.5700, 1.3),
    ("MLS_BEL_01", 13.0110, 77.5630, 1.1),
    ("MLS_SNK_01", 13.0060, 77.5740, 1.2),
    ("MLS_3MN_01", 13.0010, 77.5600, 1.2),
    ("MLS_MGR_01", 12.9960, 77.5640, 1.0),
    ("MLS_CHD_01", 12.9930, 77.5560, 0.9),
    ("MLS_10C_01", 13.0040, 77.5710, 1.3),
    ("MLS_6CR_01", 12.9970, 77.5580, 1.0),
]


def candidate_cells():
    out = []
    for idx, (cid, lat, lon, dw) in enumerate(_CANDIDATES):
        vendor = _VENDOR_CYCLE[idx % len(_VENDOR_CYCLE)]
        tx, idle, peak = _HW_5G[vendor]
        out.append({
            "cell_id": cid, "lat": lat, "lon": lon, "density_weight": dw,
            "vendor": vendor, "hardware_model": _HW_MODEL_5G[vendor],
            "band": "n78", "freq_mhz": 3500, "generation": "5G",
            "antenna_config": "64T64R", "max_ues": 900,
            "peak_dl_mbps": peak, "tx_power_w": tx, "idle_power_w": idle,
            "area": "Malleswaram",
        })
    return out


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def select_cells(user_density, budget, spectrum_bands):
    cands = candidate_cells()
    max_cells = _clamp(int(budget * 0.6 / COST_PER_SITE), 1, len(cands))
    bands = set(spectrum_bands or [])

    def score(c):
        band_factor = 1.5 if c["band"] in bands else 0.6
        return (c["density_weight"] * band_factor
                * (c["max_ues"] / 300) * (user_density / 500))

    ranked = sorted(cands, key=score, reverse=True)
    return ranked[:max_cells]


def group_dus_cus(cells, max_cells_per_du=DEFAULT_MAX_CELLS_PER_DU,
                  max_dus_per_cu=DEFAULT_MAX_DUS_PER_CU):
    """Greedy Haversine grouping from the highest-density anchor outward."""
    remaining = sorted(cells, key=lambda c: c.get("density_weight", 1.0), reverse=True)
    dus = []
    while remaining:
        anchor = remaining.pop(0)
        members = [anchor]
        remaining.sort(key=lambda c: haversine_km(anchor["lat"], anchor["lon"], c["lat"], c["lon"]))
        keep = []
        for c in remaining:
            d = haversine_km(anchor["lat"], anchor["lon"], c["lat"], c["lon"])
            if len(members) < max_cells_per_du and d <= FRONTHAUL_RADIUS_KM:
                members.append(c)
            else:
                keep.append(c)
        remaining = keep
        clat = sum(m["lat"] for m in members) / len(members)
        clon = sum(m["lon"] for m in members) / len(members)
        du_id = f"DU-BLR-{len(dus) + 1:02d}"
        for m in members:
            m["du_id"] = du_id
            d = haversine_km(clat, clon, m["lat"], m["lon"])
            m["fronthaul_latency_us"] = round(d * 5 + 10, 2)
        dus.append({
            "du_id": du_id, "cell_ids": [m["cell_id"] for m in members],
            "centroid_lat": round(clat, 6), "centroid_lon": round(clon, 6),
        })

    # group DUs under CUs
    cus = []
    for i in range(0, len(dus), max_dus_per_cu):
        chunk = dus[i:i + max_dus_per_cu]
        cu_id = f"CU-BLR-{len(cus) + 1:02d}"
        clat = sum(d["centroid_lat"] for d in chunk) / len(chunk)
        clon = sum(d["centroid_lon"] for d in chunk) / len(chunk)
        for d in chunk:
            d["cu_id"] = cu_id
            dist = haversine_km(clat, clon, d["centroid_lat"], d["centroid_lon"])
            d["midhaul_latency_ms"] = round(dist * 0.01 + 0.5, 4)
        cus.append({
            "cu_id": cu_id, "du_ids": [d["du_id"] for d in chunk],
            "centroid_lat": round(clat, 6), "centroid_lon": round(clon, 6),
        })
    return dus, cus


def estimated_cost(n_cells, n_dus, n_cus):
    return n_cells * COST_PER_CELL_USD + n_dus * COST_PER_DU_USD + n_cus * COST_PER_CU_USD


def plan_to_topology(plan):
    """Convert a plan object into a /topology/replace payload."""
    cells = {}
    for c in plan["cells"]:
        cid = c["cell_id"]
        cells[cid] = {k: v for k, v in c.items()
                      if k not in ("cell_id", "du_id", "cu_id", "fronthaul_latency_us",
                                   "slices", "slice_warnings", "density_weight")}
    dus = {d["du_id"]: {"cu_id": d["cu_id"], "host": d["du_id"].lower(),
                        "cell_ids": d["cell_ids"]} for d in plan["dus"]}
    cus = {c["cu_id"]: {"host": c["cu_id"].lower(), "region": plan.get("geographic_area", ""),
                        "du_ids": c["du_ids"]} for c in plan["cus"]}
    return {"cus": cus, "dus": dus, "cells": cells}
