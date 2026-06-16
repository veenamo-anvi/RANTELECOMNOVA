"""PCI planner (spec Appendix E.1).

Greedy graph-colouring: cells processed in descending adjacency-degree order. A PCI
is valid for a cell if, among assigned neighbours, it is collision-free (no shared
PCI) and confusion-free (no shared PCI mod 3); the smallest such PCI is chosen.
Fallback: smallest collision-free PCI when confusion is unavoidable.
"""
import math

PCI_MAX = 1007
ADJACENCY_RADIUS_KM = 3.0
EARTH_R_KM = 6371.0


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def build_adjacency(cells):
    """cells: list of dicts with cell_id, lat, lon -> {cell_id: set(neighbour ids)}."""
    adj = {c["cell_id"]: set() for c in cells}
    for i, a in enumerate(cells):
        for b in cells[i + 1:]:
            if haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) <= ADJACENCY_RADIUS_KM:
                adj[a["cell_id"]].add(b["cell_id"])
                adj[b["cell_id"]].add(a["cell_id"])
    return adj


def assign_pcis(cells):
    """Return (pci_by_cell, violations). cells: list of dicts (cell_id, lat, lon)."""
    adj = build_adjacency(cells)
    order = sorted(cells, key=lambda c: len(adj[c["cell_id"]]), reverse=True)
    pci = {}
    violations = []
    for c in order:
        cid = c["cell_id"]
        nb_pcis = {pci[n] for n in adj[cid] if n in pci}
        nb_mod3 = {pci[n] % 3 for n in adj[cid] if n in pci}
        chosen = None
        for p in range(0, PCI_MAX + 1):
            if p not in nb_pcis and (p % 3) not in nb_mod3:
                chosen = p
                break
        if chosen is None:  # confusion unavoidable -> smallest collision-free
            for p in range(0, PCI_MAX + 1):
                if p not in nb_pcis:
                    chosen = p
                    break
            violations.append(
                f"{cid}: confusion-free PCI unavailable among {len(adj[cid])} neighbours; "
                f"assigned collision-free PCI {chosen}"
            )
        pci[cid] = chosen if chosen is not None else 0
    return pci, violations
