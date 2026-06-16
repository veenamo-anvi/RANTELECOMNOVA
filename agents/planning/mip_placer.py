"""MIP-optimal placement (spec Appendix E.4 — Almoghathawi et al., 2024).

Minimise total network cost (CAPEX z + OPEX y) subject to single-build, coverage,
activation, unique-assignment, implies-active, capacity, and SINR-QoS constraints.
CBC via pulp; falls back to the heuristic select_cells() on non-Optimal status.
"""
import math
from dataclasses import dataclass, field

import pulp

from placement import candidate_cells, select_cells, group_dus_cus


@dataclass
class PropagationParams:
    h_tx: float = 25.0
    h_rx: float = 2.0
    h_bld: float = 10.0
    b_sep: float = 50.0
    w_street: float = 25.0
    phi: float = 30.0
    metropolitan: bool = True
    min_rx_power_dbm: float = -100.0
    sinr_min_db: float = 10.0
    noise_power_dbm: float = -120.0


# per-band TX -> (tx_dbm, ant_dbi)
_BAND_TX = {
    "n78": (43, 18), "n41": (43, 18), "n28": (43, 18),
    "B3": (40, 15), "B40": (40, 15),
}

# Demand clusters (lat, lon, n_channels ~ Erlangs), one per DU zone
BANGALORE_DEMAND_CLUSTERS = {
    "DC-MLS-N": (13.0070, 77.5700, 160),
    "DC-MLS-C": (13.0015, 77.5650, 150),
    "DC-MLS-S": (12.9955, 77.5595, 150),
}

# multi-period profiles
CASE_A = [["DC-MLS-C"], ["DC-MLS-N", "DC-MLS-S"]]                      # permanent/expanding
CASE_B = [["DC-MLS-N"], ["DC-MLS-C", "DC-MLS-S"],
          ["DC-MLS-N", "DC-MLS-C", "DC-MLS-S"]]                        # temporary/shifting


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def walfisch_ikegami_pl(dist_km, freq_mhz, pp: PropagationParams):
    """COST-231 Walfisch-Ikegami path loss (dB), urban NLOS."""
    d = max(dist_km, 0.02)
    f = freq_mhz
    Lfs = 32.4 + 20 * math.log10(d) + 20 * math.log10(f)
    dhm = pp.h_bld - pp.h_rx
    Lrts = -16.9 - 10 * math.log10(pp.w_street) + 10 * math.log10(f) + 20 * math.log10(dhm) + _l_ori(pp.phi)
    dhb = pp.h_tx - pp.h_bld
    Lbsh = -18 * math.log10(1 + dhb) if pp.h_tx > pp.h_bld else 0.0
    if pp.h_tx > pp.h_bld:
        ka = 54.0
    else:
        ka = 54.0 - 0.8 * dhb  # simplified for d>=0.5km
    kd = 18.0 if pp.h_tx > pp.h_bld else 18.0 - 15 * dhb / pp.h_bld
    if pp.metropolitan:
        kf = -4 + 1.5 * (f / 925 - 1)
    else:
        kf = -4 + 0.7 * (f / 925 - 1)
    Lmsd = Lbsh + ka + kd * math.log10(d) + kf * math.log10(f) - 9 * math.log10(pp.b_sep)
    L = Lfs + max(0.0, Lrts + Lmsd)
    return L


def _l_ori(phi):
    if phi < 35:
        return -10 + 0.354 * phi
    if phi < 55:
        return 2.5 + 0.075 * (phi - 35)
    return 4.0 - 0.114 * (phi - 55)


def compute_link_powers(sites, clusters, pp: PropagationParams):
    """Return {(cluster_id, site_id): rx_power_dbm} for all pairs."""
    powers = {}
    for cid, (clat, clon, _) in clusters.items():
        for s in sites:
            tx_dbm, ant_dbi = _BAND_TX.get(s.get("band", "n78"), (43, 18))
            d = _haversine_km(clat, clon, s["lat"], s["lon"])
            pl = walfisch_ikegami_pl(d, s.get("freq_mhz", 3500), pp)
            powers[(cid, s["cell_id"])] = tx_dbm + ant_dbi - pl
    return powers


def _dbm_to_mw(dbm):
    return 10 ** (dbm / 10)


def select_cells_mip(budget, spectrum_bands, period_clusters, sinr_min_db=10.0,
                     time_limit_sec=120):
    """Multi-period MIP. period_clusters: list[list[cluster_id]] (one per period).

    Returns dict with status, selected sites, build_schedule, costs, feasibility.
    """
    sites = candidate_cells()
    max_sites = len(sites)
    install_cost = budget * 0.6 / max_sites
    op_cost = 1000.0
    pp = PropagationParams(sinr_min_db=sinr_min_db)

    powers = compute_link_powers(sites, BANGALORE_DEMAND_CLUSTERS, pp)
    site_ids = [s["cell_id"] for s in sites]
    site_cap = {s["cell_id"]: s["max_ues"] for s in sites}
    periods = list(range(len(period_clusters)))

    # feasible set S(i): sites whose rx power at cluster i >= min_rx_power
    feasible = {}
    for cid in BANGALORE_DEMAND_CLUSTERS:
        feasible[cid] = [sid for sid in site_ids
                         if powers[(cid, sid)] >= pp.min_rx_power_dbm]

    prob = pulp.LpProblem("bs_placement", pulp.LpMinimize)
    z = pulp.LpVariable.dicts("z", (site_ids, periods), cat="Binary")  # build
    y = pulp.LpVariable.dicts("y", (site_ids, periods), cat="Binary")  # active
    x = {}  # assign demand i->j in period t (only feasible pairs)
    for t in periods:
        for i in period_clusters[t]:
            for j in feasible.get(i, []):
                x[(i, j, t)] = pulp.LpVariable(f"x_{i}_{j}_{t}", cat="Binary")

    # objective: Σ c·z + r·y
    prob += pulp.lpSum(install_cost * z[j][t] + op_cost * y[j][t]
                       for j in site_ids for t in periods)

    # (2) single-build
    for j in site_ids:
        prob += pulp.lpSum(z[j][t] for t in periods) <= 1
    # (4) activation: active only after built in this or earlier period
    for j in site_ids:
        for t in periods:
            prob += y[j][t] <= pulp.lpSum(z[j][tp] for tp in periods if tp <= t)
    # (3)+(5) coverage & unique assignment
    for t in periods:
        for i in period_clusters[t]:
            feas = feasible.get(i, [])
            if not feas:
                continue
            prob += pulp.lpSum(x[(i, j, t)] for j in feas) == 1
            # (6) implies-active
            for j in feas:
                prob += x[(i, j, t)] <= y[j][t]
    # (7) capacity
    for t in periods:
        for j in site_ids:
            assigned = [x[(i, j, t)] * BANGALORE_DEMAND_CLUSTERS[i][2]
                        for i in period_clusters[t] if (i, j, t) in x]
            if assigned:
                prob += pulp.lpSum(assigned) <= site_cap[j]
    # (8) SINR QoS, linearised
    sinr_lin = _dbm_to_mw(sinr_min_db)  # 10^(sinr_min/10)
    p_noise = _dbm_to_mw(pp.noise_power_dbm)
    for t in periods:
        for i in period_clusters[t]:
            feas = feasible.get(i, [])
            if not feas:
                continue
            alpha = pulp.lpSum(_dbm_to_mw(powers[(i, j)]) * x[(i, j, t)] for j in feas)
            beta = pulp.lpSum(_dbm_to_mw(powers[(i, j)]) * y[j][t] for j in feas)
            prob += alpha * (1 + sinr_lin) >= sinr_lin * p_noise + sinr_lin * beta

    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit_sec)
    prob.solve(solver)
    status = pulp.LpStatus[prob.status]

    if status != "Optimal":
        fb = select_cells(500, budget, spectrum_bands)
        return {
            "status": status, "source": "heuristic_fallback",
            "selected_cells": fb, "build_schedule": {},
            "install_cost": install_cost, "op_cost": op_cost,
            "total_cost": None, "feasibility": feasible,
        }

    build_schedule = {}
    selected = []
    for j in site_ids:
        for t in periods:
            if pulp.value(z[j][t]) and pulp.value(z[j][t]) > 0.5:
                build_schedule[j] = t
                selected.append(next(s for s in sites if s["cell_id"] == j))
    total = pulp.value(prob.objective)
    return {
        "status": status, "source": "mip",
        "selected_cells": selected, "build_schedule": build_schedule,
        "install_cost": round(install_cost, 2), "op_cost": op_cost,
        "total_cost": round(total, 2) if total is not None else None,
        "feasibility": feasible,
    }
