# Runbook — running the stack on a fresh machine

Everything runs in Docker. No Python/Node needed on the host except (optionally) to
use the `chat.py` CLI.

## Prerequisites

- **Docker Desktop** running (includes Docker Compose v2). Allocate ~4–6 GB RAM.
- **git**
- *(optional)* A **Google Gemini API key** — only needed for the orchestrator chat.
  Everything else (simulators, controller, planning, map, Grafana) runs without it.

## 1. Clone

```bash
git clone https://github.com/veenamo-anvi/RANTELECOMNOVA.git
cd RANTELECOMNOVA/dev-env
```

## 2. Configure environment

```bash
cp .env.example .env        # Windows PowerShell: copy .env.example .env
```

Edit `dev-env/.env` and set at minimum:
- `INFLUXDB_TOKEN` — any long random string (e.g. `dev-token-12345abcde`)
- `INFLUXDB_ADMIN_PASSWORD`, `GRAFANA_PASSWORD` — any passwords
- `GOOGLE_API_KEY` — your Gemini key (only if you want orchestrator chat)

If host port 8086 is busy, also set `INFLUX_HOST_PORT=8087` (or any free port).

## 3. Build & start (all 12 containers)

```bash
docker compose up --build
```

First build takes ~5–10 min (the kpi-agent image pulls CPU PyTorch). Add `-d` to run
detached. Watch for `influxdb ... healthy`, then the agents starting.

## 4. Open the UIs

| What | URL | Notes |
|---|---|---|
| Live cell map | http://localhost:8083 | 30 cells, vendor colours, click for KPIs |
| Grafana | http://localhost:3000 | login `admin` / `GRAFANA_PASSWORD`; 5 dashboards under folder **Telecom** |
| Controller API | http://localhost:8080/network | full topology + live KPIs |
| Planning API | http://localhost:8081/health | |
| Orchestrator | http://localhost:8082/health | shows active model |

## 5. Smoke tests

```bash
# 30 cells streaming with live KPIs (data appears ~10–20 s after start)
curl http://localhost:8080/network

# generate a network plan (heuristic)
curl -X POST http://localhost:8081/plan -H "Content-Type: application/json" -d "{}"

# MIP-optimal plan
curl -X POST http://localhost:8081/plan -H "Content-Type: application/json" -d "{\"use_mip\":true}"

# orchestrator chat (needs GOOGLE_API_KEY)
curl -X POST http://localhost:8082/chat -H "Content-Type: application/json" \
  -d "{\"message\":\"summarise the status of all cells\",\"session_id\":\"demo\"}"
```

Operator CLI (needs Python 3 on the host; pure stdlib, no installs):

```bash
cd ..            # repo root
python chat.py   # talks to localhost:8082; try /status /alerts /cells /plan
```

## 6. Demo scenario (end to end)

1. Open the map (`:8083`) — confirm all 30 cells render with live KPIs.
2. In `chat.py`, run `/status` then `/alerts` — the KPI agent emits SON actions as
   load shifts (overload → cell move + LOAD_BALANCE within ~60 s).
3. `/plan` to generate a network plan; in Grafana watch the dashboards update.

## 7. Stop / clean up

```bash
docker compose down        # stop & remove containers (keeps data volumes)
docker compose down -v      # also wipe InfluxDB/Grafana data
```

## Troubleshooting

- **Port already allocated** — another service owns 8080/8081/8082/8083/8086/3000.
  Stop it, or change the left-hand host port in `docker-compose.yml` (and
  `INFLUX_HOST_PORT` for influx).
- **No data in map/Grafana** — give it 20–30 s after `healthy`; check
  `docker compose logs du-mls-1` for write errors and that `INFLUXDB_TOKEN` matches
  in `.env`.
- **Orchestrator `[Error] quota`** — Gemini key missing/rate-limited; the rest of the
  stack is unaffected.
```
