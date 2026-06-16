# Telecom Network Automation — Malleswaram O-RAN

Multi-agent Agentic-AI system that plans, configures, deploys and continuously
optimises an O-RAN-compliant 4G/5G NSA network over a simulated Malleswaram
(North Bangalore) deployment. See [spec.md](spec.md) for the full design and
[plan.md](plan.md) for the build plan.

## Architecture (12 containers)

| Service | Port | Role |
|---|---|---|
| influxdb | 8086 | time-series KPI store |
| grafana | 3000 | 5 provisioned dashboards |
| core-sim | — | AMF/SMF/UPF telemetry |
| cu-mls | — | CU (RRC/PDCP) telemetry |
| du-mls-1/2/3 | — | DU (4G+5G RAN) telemetry |
| controller | 8080 | topology source of truth + KPI merge |
| planning-api | 8081 | heuristic + MIP planning |
| kpi-agent | — | BiLSTM anomaly detection + SON |
| orchestrator | 8082 | LLM chat (Gemini / Claude CLI) |
| map-server | 8083 | Leaflet live cell map |

## Quick start

```bash
cd dev-env
cp .env.example .env          # fill in INFLUXDB_TOKEN, GOOGLE_API_KEY, etc.
docker compose up --build
```

Then:
- Live map → http://localhost:8083
- Grafana → http://localhost:3000 (admin / $GRAFANA_PASSWORD)
- Controller API → http://localhost:8080/network
- Planning API → http://localhost:8081/health
- Orchestrator → http://localhost:8082/health

Operator CLI (from the project root):

```bash
py chat.py                    # talks to localhost:8082
```

## Regenerating derived files

```bash
python dev-env/config/_gen_seed.py          # rebuild topology.json seed
python dev-env/grafana/_gen_dashboards.py   # rebuild the 5 dashboards
python agents/kpi_agent/train.py            # train kpi_model.pt (auto on first boot)
```

## Layout

```
agents/        controller, planning, kpi_agent, orchestrator, map_server
dev-env/       docker-compose.yml, config/topology.json, simulators/, grafana/
chat.py        operator CLI client
```

## Notes / known spec divergences

- CU/Core use a distinct diurnal curve and no weekend factor.
- SON SINR alert `alert_type` is `SINR_DEGRADATION` (not the model class `SINR_LOW`).
- Heuristic candidate vendor cycle / Ericsson idle power (240 W) differ from the
  deployed seed (per-site vendor; 237 W) — see spec E.3.
- Applying a regenerated plan renames live DU/CU ids (`DU-BLR-NN`); simulators follow
  whatever ids the applied plan writes.
