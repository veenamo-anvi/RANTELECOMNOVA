# Implementation Plan — Telecom Network Automation

Derived from [spec.md](spec.md). This plan sequences the build of the 12-container
multi-agent O-RAN system into ordered phases, each with concrete deliverables and
acceptance checks. Build order respects runtime dependencies: data store →
topology seed → simulators → control plane → planning → ML agent → orchestrator →
map → dashboards → compose wiring → CLI.

## Target repository layout

```
RAN_BUILD/
├── chat.py                                  # operator CLI (Phase 10)
├── agents/
│   ├── controller/      controller.py, requirements.txt, Dockerfile
│   ├── planning/        planning.py, placement.py, pci_planner.py,
│   │                    slice_allocator.py, mip_placer.py, requirements.txt, Dockerfile
│   ├── kpi_agent/       kpi_agent.py, model.py, train.py, requirements.txt, Dockerfile
│   ├── orchestrator/    orchestrator.py, tools.py, requirements.txt, Dockerfile
│   └── map_server/      map_server.py, static/, requirements.txt, Dockerfile
└── dev-env/
    ├── docker-compose.yml
    ├── .env.example
    ├── config/topology.json                 # canonical seed (A.5)
    ├── simulators/
    │   ├── du/   du_simulator.py, requirements.txt, Dockerfile
    │   ├── cu/   cu_simulator.py, requirements.txt, Dockerfile
    │   └── core/ core_simulator.py, requirements.txt, Dockerfile
    └── grafana/
        ├── provisioning/datasources/influxdb.yml
        ├── provisioning/dashboards/default.yaml
        └── dashboards/*.json                # 5 dashboards (H.6)
```

## Conventions / shared facts

- Python 3.12-slim base for all code services. Pinned deps per spec H.3.
- InfluxDB org `telecom`, bucket `telecom_metrics`. Measurements in A.4.
- All simulators push every `INTERVAL_SEC=10s`; DU/CU poll topology every `TOPO_POLL_SEC=5s`.
- Topology source of truth: `dev-env/config/topology.json`; Controller is the only writer
  (atomic `.tmp`→rename); simulators read-only.
- Cell naming `MLS_<SITE>_<SECTOR>`; 30 cells, 10 sites × 3 sectors.

---

## Phase 0 — Scaffolding & shared data

**Deliverables**
- Full directory tree above (empty packages with `__init__` where useful).
- `dev-env/config/topology.json` — canonical seed per **A.5 + B.1/B.2**: version 1,
  `updated_by="seed"`, meta block (A.2), 1 CU, 3 DUs (12/9/9 grouping), 30 cells with
  intrinsic attributes (vendor, band, freq, pci, hw, antenna, peak_dl, tx/idle power, max_ues, coords).
- `dev-env/.env.example` with all H.1 vars.

**Acceptance**: `topology.json` validates as JSON; 30 cells present; vendor counts
Nokia 9 / Ericsson 9 / Samsung 6 / ZTE 6; PCI bands per B.2; max_ues sum = 16,500.

## Phase 1 — Simulators (data producers)

Order: DU → CU → Core (DU is the richest; CU/Core reuse patterns).

**1a. DU simulator** (`dev-env/simulators/du/du_simulator.py`) — C.1
- COST-231-Hata coverage radius; per-band `_BAND_PARAMS`, `_ANT_GAIN`, `_RF_EFF`.
- Population/demand model (AREA_DENSITY, MARKET_SHARE, PEAK_CONCURRENT).
- Diurnal `HOURLY_LOAD` + weekend factor; per-tick UE count.
- `cell_kpi` field formulas (all 19 fields incl. `ho_success_rate`).
- `du_kpi`, `ue_mobility` (physical UE move), `ue_usage` (≤8 UEs/tick).
- Reads topology every `TOPO_POLL_SEC`, serves cells listed under its `DU_ID`.

**1b. CU simulator** (`.../cu/cu_simulator.py`) — C.2: own diurnal curve, no weekend
factor; `cu_kpi` fields.

**1c. Core simulator** (`.../core/core_simulator.py`) — C.3: topology-independent;
3 `core_kpi` points/tick (AMF/SMF/UPF) with exponential smoothing.

**Acceptance**: each writes its measurements to InfluxDB on a 10 s cadence; DU radius
& UE counts within expected ranges; reconfigures within 5 s of a topology edit.

## Phase 2 — Controller (control plane / source of truth)

`agents/controller/controller.py` — FastAPI :8080 — F.1
- Read endpoints: `/health`, `/topology`, `/network`, `/cells`, `/cells/{id}`, `/dus`, `/cus`.
- `latest_*_kpis()` helpers (Flux `-5m` pivot; `-30m` series for cell detail).
- Live `du_id`/`cu_id` resolution from topology.
- Mutations (atomic write + `topology_event`): `/move/cell`, `/move/du`,
  `/topology/replace`, `/cells/add` (auto-PCI), `DELETE /cells/{id}`.
- `/neighbors/{id}` (Haversine), `/son/pci-reopt` (collision/confusion-free PCI, E.1).

**Acceptance**: `/network` merges topology + latest KPIs for all 30 cells; moves persist
to `topology.json` and propagate to simulators within 5 s; pci-reopt only writes on change.

## Phase 3 — Planning engine

`agents/planning/` — FastAPI :8081 — E + F.2
- `placement.py`: CANDIDATE_CELLS (E.3), density-weighted heuristic, DU/CU greedy grouping,
  cost model, fronthaul/midhaul latency, `plan_to_topology()`.
- `pci_planner.py`: greedy graph-colouring, ADJACENCY_RADIUS_KM=3, collision+confusion-free
  with collision-free fallback (E.1).
- `slice_allocator.py`: MIN_PRB floors, latency targets, timing-sync strategy (E.2).
- `mip_placer.py`: CBC/pulp MIP (Almoghathawi), Walfisch-Ikegami link budget, demand clusters,
  constraint (8) linearisation, heuristic fallback (E.4).
- Endpoints: `/plan`, `/plan/multi-period`, `/plan/apply`, `/plan/{id}`, `/demand-clusters`,
  `/candidates`, `/health`.

**Acceptance**: `/plan` returns conflict-free plan (<30 s) with summary; `use_mip=true`
produces a build schedule; `/plan/apply` POSTs to Controller `/topology/replace`.

## Phase 4 — KPI monitoring agent

`agents/kpi_agent/` — background — D
- `model.py`: `KPIClassifier` BiLSTM (D.1), `FEATURE_NORM` (D.2).
- `train.py`: synthetic dataset (D.4 means/stds), hyperparams (D.3), reproducible seed,
  writes `kpi_model.pt`.
- `kpi_agent.py`: 6-step sliding window, rule-based fallback (D.7), AI gate (conf≥0.70),
  SON actions per class (D.8), alerts + son_actions vocab (D.9), thresholds (D.6).

**Acceptance**: trains on first boot if model absent; detects overload within 2 cycles
(60 s) and issues a cell-move LOAD_BALANCE; writes `alerts`/`son_actions`.

## Phase 5 — Orchestrator

`agents/orchestrator/` — FastAPI :8082 — 6.1 + F.3 + G
- `tools.py`: 13 Anthropic-style schemas; `_clean_params()` → Gemini `function_declarations`;
  `TOOL_MAP` HTTP/Influx implementations.
- `orchestrator.py`: Gemini (default) + Claude-CLI backends; `build_network_context()`;
  streaming tool-calling loop; in-memory sessions; `/chat`, `/history`, `/tools`, `/health`.

**Acceptance**: `/chat` streams, calls tools, returns network status; backend selectable
via `CLAUDE_CLI_PATH`; quota errors surfaced as `[Error]`.

## Phase 6 — Map server

`agents/map_server/` — FastAPI :8083 — 6.5 + F.4
- `GET /` Leaflet page (30 s refresh); `/api/cells` proxies Controller `/network`;
  vendor colours, 5G/4G styling, overload/SINR fills, click popups.

**Acceptance**: renders all 30 cells with live KPI overlays within 5 s of controller start.

## Phase 7 — Grafana provisioning + dashboards

`dev-env/grafana/` — H.5/H.6: datasource `InfluxDB-Telecom`, dashboard provider,
5 dashboards (network_overview, cell_kpi, du_cu_performance, son_alerts, ue_analytics).

**Acceptance**: dashboards auto-load against the Flux datasource and render live data.

## Phase 8 — Dockerfiles & per-service requirements

Common pattern (H.2); kpi-agent CPU-Torch layer; orchestrator Node 20 + claude-code.
Pinned deps per H.3.

## Phase 9 — Compose wiring

`dev-env/docker-compose.yml` — H.4: 12 services, ports, `depends_on` (influx healthy),
topology mounts (rw controller / ro sims), per-service env, named volumes, Grafana provisioning.

**Acceptance**: `docker compose up` brings all 12 up healthy; data flows end-to-end.

## Phase 10 — Operator CLI

`chat.py` (root) — 6.6: stdlib REPL → orchestrator; built-in commands; `--url`/`--session`.

## Phase 11 — End-to-end validation (Part III success criteria)

- Plan generated <30 s; apply propagates <10 s.
- KPI agent reacts to overload within 60 s.
- Orchestrator routes ≥90% of test commands.
- 30 cells stream with zero gaps; map renders all 30 within 5 s.

---

## Build order summary

`0 → 1 → 2 → 3 → 4 → 5 → 6 → 7` (code) then `8 → 9` (containerise) then `10 → 11`
(client + validate). Phases 3–6 depend on Controller (2); all simulators/agents depend
on InfluxDB and the topology seed (0/1).

## Open risks / divergences to honour

- CU/Core use a **distinct** diurnal curve and no weekend factor (C.2 ⚠).
- SINR alert `alert_type` is `SINR_DEGRADATION`, not `SINR_LOW` (D.9 ⚠).
- CANDIDATE_CELLS vendor cycle + Ericsson idle 240 W differ from deployed seed 237 W (E.3 ⚠).
- Planner emits `DU-BLR-NN`/`CU-BLR-NN`; applying a regenerated plan renames live DU/CU ids;
  simulators follow applied ids (C / E.3).
- `GEMINI_MODEL` in-code default `gemini-2.0-flash`, compose overrides to `gemini-2.5-flash` (G).
