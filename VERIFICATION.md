# Verification Notes

Records what has been verified against running/executed code, and how. For the
full acceptance checklist see [STATUS.md](STATUS.md).

## Map UI chat panel — verified 2026-06-17

**Setup.** Ran the map server (`agents/map_server/map_server.py`, commit `bd66847`)
on host port 8090 via uvicorn, with `CONTROLLER_URL` and `ORCHESTRATOR_URL` pointed
at a live controller (`:8080`) and a live Gemini-backed orchestrator (`:8082`,
model `gemini-2.5-flash`). The orchestrator was used purely as the answer engine —
the code under test is the map server's chat panel + `/chat` proxy.

**Results.**

| Layer | Test | Result |
|---|---|---|
| Page UI | `GET /` | Chat panel present (`#chat`, `#chatlog`, `#chatform`, shortcut buttons, "Network Assistant") |
| Cell proxy | `GET /api/cells` | 30 live cells returned (e.g. `MLS_RWS_01`) |
| Chat proxy | `POST /chat` (streaming) | Real model reply streamed back, including the `*[calling tool: query_network...]*` marker — confirms map → orchestrator → LLM → tool-call → streamed response end to end |

**Caveats.**
- The model's answer reflected the live controller's snapshot at test time
  ("PRB data not available"); this is backend data, not a chat-panel fault.
- A valid `GOOGLE_API_KEY` is required in `dev-env/.env` for the orchestrator to
  produce real answers; without it the panel still streams an `[Error] quota/API`
  message, which confirms the wiring.

## Previously verified (no containers)

- `topology.json` seed: 30 cells, vendors 9/9/6/6, max_ues 16,500, DU grouping 12/9/9.
- Planning pure modules: 10-cell selection, PCI graph-colouring + collision-free
  fallback, DU/CU grouping, slice fractions sum to 1.0, timing-sync strategy.
- MIP placer (pulp/CBC): Optimal status, build schedule, cost.
- KPI model: 10,000-sequence dataset, correct class balance, valid softmax inference.
- Orchestrator: 13 tools, Gemini schema translation strips defaults / empty enums.
- All Python modules compile; Grafana JSON valid; `docker compose config` passes.
- Data plane built and ran under Docker (InfluxDB + simulators + controller + planning);
  validated host port handling (`INFLUX_HOST_PORT`).

## Pending (needs full `docker compose up` on a free machine)

Part III success criteria in [STATUS.md](STATUS.md): plan < 30 s, apply propagation
< 10 s, KPI overload reaction < 60 s, 30 cells gap-free, map render < 5 s.
