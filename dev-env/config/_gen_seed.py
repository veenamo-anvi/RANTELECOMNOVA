"""Generate the canonical topology.json seed (spec A.5 + A.2 + B.1 + B.2).

Run:  python _gen_seed.py   ->  writes topology.json next to this file.
The seed is the §5 initial design (12/9/9 grouping), version 1, updated_by "seed".
"""
import json
import os

# --- B.1 site coordinates (10 macro towers) ---------------------------------
SITES = {
    "RWS": (13.007, 77.576),
    "18C": (13.004, 77.566),
    "BEL": (13.011, 77.562),
    "SNK": (13.004, 77.574),
    "SPG": (13.000, 77.571),
    "3MN": (13.000, 77.558),
    "10C": (13.003, 77.570),
    "MGR": (12.996, 77.562),
    "CHD": (12.994, 77.556),
    "6CR": (12.997, 77.553),
}

# --- Hardware model lookup by (vendor, generation) --------------------------
HW = {
    ("Nokia", "5G"): "AirScale MAA 64T64R",
    ("Nokia", "4G"): "AWHFA",
    ("Ericsson", "5G"): "AIR 6449",
    ("Ericsson", "4G"): "RBS 6402",
    ("Samsung", "5G"): "TM500 64T64R",
    ("Samsung", "4G"): "RRU",
    ("ZTE", "5G"): "AAU 5614",
    ("ZTE", "4G"): "RRU",
}

# --- B.2 full 30-cell inventory ---------------------------------------------
# (cell_id, gen, band, freq, pci, vendor, peak_dl, tx_w, idle_w, max_ues)
CELLS = [
    ("MLS_RWS_01", "5G", "n78", 3500,   1, "Nokia",    3800, 1000, 250, 900),
    ("MLS_RWS_02", "5G", "n41", 2500, 101, "Nokia",    3000, 1000, 250, 700),
    ("MLS_RWS_03", "4G", "B3",  1800, 201, "Nokia",     150,  200,  50, 250),
    ("MLS_18C_01", "5G", "n78", 3500,   2, "Ericsson", 3600,  950, 237, 900),
    ("MLS_18C_02", "5G", "n41", 2500, 102, "Ericsson", 2800,  950, 237, 700),
    ("MLS_18C_03", "4G", "B3",  1800, 202, "Ericsson",  150,  200,  50, 250),
    ("MLS_BEL_01", "5G", "n78", 3500,   3, "Samsung",  3400,  900, 225, 900),
    ("MLS_BEL_02", "4G", "B40", 2300, 301, "Samsung",   150,  200,  50, 300),
    ("MLS_BEL_03", "4G", "B3",  1800, 203, "Samsung",   150,  200,  50, 250),
    ("MLS_SNK_01", "5G", "n78", 3500,   4, "ZTE",      3200, 1000, 250, 900),
    ("MLS_SNK_02", "5G", "n41", 2500, 103, "ZTE",      2600, 1000, 250, 700),
    ("MLS_SNK_03", "4G", "B3",  1800, 204, "ZTE",       150,  200,  50, 250),
    ("MLS_SPG_01", "5G", "n78", 3500,   5, "Nokia",    3800, 1000, 250, 900),
    ("MLS_SPG_02", "5G", "n41", 2500, 104, "Nokia",    3000, 1000, 250, 700),
    ("MLS_SPG_03", "4G", "B3",  1800, 205, "Nokia",     150,  200,  50, 250),
    ("MLS_3MN_01", "5G", "n78", 3500,   6, "Ericsson", 3600,  950, 237, 900),
    ("MLS_3MN_02", "4G", "B40", 2300, 302, "Ericsson",  150,  200,  50, 300),
    ("MLS_3MN_03", "4G", "B3",  1800, 206, "Ericsson",  150,  200,  50, 250),
    ("MLS_10C_01", "5G", "n78", 3500,   7, "Samsung",  3400,  900, 225, 900),
    ("MLS_10C_02", "5G", "n41", 2500, 105, "Samsung",  2400,  900, 225, 700),
    ("MLS_10C_03", "4G", "B3",  1800, 207, "Samsung",   150,  200,  50, 250),
    ("MLS_MGR_01", "5G", "n78", 3500,   8, "ZTE",      3200, 1000, 250, 900),
    ("MLS_MGR_02", "4G", "B40", 2300, 303, "ZTE",       150,  200,  50, 300),
    ("MLS_MGR_03", "4G", "B3",  1800, 208, "ZTE",       150,  200,  50, 250),
    ("MLS_CHD_01", "5G", "n78", 3500,   9, "Nokia",    3800, 1000, 250, 900),
    ("MLS_CHD_02", "4G", "B40", 2300, 304, "Nokia",     150,  200,  50, 300),
    ("MLS_CHD_03", "4G", "B3",  1800, 209, "Nokia",     150,  200,  50, 250),
    ("MLS_6CR_01", "5G", "n78", 3500,  10, "Ericsson", 3600,  950, 237, 900),
    ("MLS_6CR_02", "4G", "B40", 2300, 305, "Ericsson",  150,  200,  50, 300),
    ("MLS_6CR_03", "4G", "B3",  1800, 210, "Ericsson",  150,  200,  50, 250),
]

# --- A.5 DU -> cell grouping (12/9/9 initial design) ------------------------
DU_CELLS = {
    "DU-MLS-1": [c[0] for c in CELLS if c[0][4:7] in ("RWS", "18C", "BEL", "SNK")],
    "DU-MLS-2": [c[0] for c in CELLS if c[0][4:7] in ("SPG", "3MN", "10C")],
    "DU-MLS-3": [c[0] for c in CELLS if c[0][4:7] in ("MGR", "CHD", "6CR")],
}

# --- A.2 meta block ---------------------------------------------------------
META = {
    "city": "Bangalore",
    "zone": "Malleswaram",
    "areas": ["Malleswaram"],
    "area_population": 40000,
    "area_population_with_commuters": 46000,
    "commuter_overhead_pct": 15,
    "operator_market_share_pct": 40,
    "active_ues_peak": 18400,
    "erlang_busy_hour": 460,
    "erlang_per_cell_target": 16,
    "cells_required": 30,
    "ran_mode": "4G/5G NSA",
    "core": "NSA — 5G NR anchored on LTE, shared AMF/SMF/UPF",
    "note": (
        "30 cells (10 macro towers x 3 sectors each) covering Malleswaram. "
        "Population 40,000 + 15% commuter overhead (railway station transit hub) "
        "= 46,000. Tier-1 operator 40% market share -> 18,400 peak active UEs. "
        "460 Erlangs busy-hour / 16 Erlangs per sector (Erlang-C, 2% blocking) "
        "= 28.75 -> 30 sectors."
    ),
}


def build():
    cells = {}
    for cid, gen, band, freq, pci, vendor, peak_dl, tx_w, idle_w, max_ues in CELLS:
        site = cid[4:7]
        lat, lon = SITES[site]
        cells[cid] = {
            "area": "Malleswaram",
            "lat": lat,
            "lon": lon,
            "generation": gen,
            "band": band,
            "freq_mhz": freq,
            "pci": pci,
            "vendor": vendor,
            "hardware_model": HW[(vendor, gen)],
            "antenna_config": "64T64R" if gen == "5G" else "4T4R",
            "peak_dl_mbps": peak_dl,
            "tx_power_w": tx_w,
            "idle_power_w": idle_w,
            "max_ues": max_ues,
        }

    topo = {
        "version": 1,
        "last_updated": "2026-06-11T00:00:00+00:00",
        "updated_by": "seed",
        "meta": META,
        "cus": {
            "CU-MLS": {
                "host": "cu-mls",
                "region": "Malleswaram",
                "du_ids": ["DU-MLS-1", "DU-MLS-2", "DU-MLS-3"],
            }
        },
        "dus": {
            "DU-MLS-1": {"cu_id": "CU-MLS", "host": "du-mls-1", "cell_ids": DU_CELLS["DU-MLS-1"]},
            "DU-MLS-2": {"cu_id": "CU-MLS", "host": "du-mls-2", "cell_ids": DU_CELLS["DU-MLS-2"]},
            "DU-MLS-3": {"cu_id": "CU-MLS", "host": "du-mls-3", "cell_ids": DU_CELLS["DU-MLS-3"]},
        },
        "cells": cells,
    }
    return topo


if __name__ == "__main__":
    topo = build()
    out = os.path.join(os.path.dirname(__file__), "topology.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(topo, f, indent=2)
        f.write("\n")

    # --- sanity checks (spec invariants) ---
    cells = topo["cells"]
    assert len(cells) == 30, f"expected 30 cells, got {len(cells)}"
    vc = {}
    for c in cells.values():
        vc[c["vendor"]] = vc.get(c["vendor"], 0) + 1
    assert vc == {"Nokia": 9, "Ericsson": 9, "Samsung": 6, "ZTE": 6}, vc
    assert sum(c["max_ues"] for c in cells.values()) == 16500
    assert (len(topo["dus"]["DU-MLS-1"]["cell_ids"]),
            len(topo["dus"]["DU-MLS-2"]["cell_ids"]),
            len(topo["dus"]["DU-MLS-3"]["cell_ids"])) == (12, 9, 9)
    print(f"OK: wrote {out}")
    print(f"  cells=30  vendors={vc}  max_ues_sum=16500  grouping=12/9/9")
