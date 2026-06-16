"""Orchestrator (spec §6.1 + F.3 + Appendix G) — FastAPI :8082.

Two interchangeable LLM backends selected by CLAUDE_CLI_PATH: Gemini (default,
google-genai) and Claude CLI. Drives a streaming multi-step tool-calling loop.
"""
import json
import os
import subprocess

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import tools as T

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://controller:8080")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "")
ANTHROPIC_MODEL_NAME = os.getenv("ANTHROPIC_MODEL_NAME", "sonnet")

USE_CLAUDE = bool(CLAUDE_CLI_PATH)
ACTIVE_MODEL = (f"claude/{ANTHROPIC_MODEL_NAME}" if USE_CLAUDE else GEMINI_MODEL)

SYSTEM_PROMPT = """You are the operations orchestrator for an O-RAN 4G/5G NSA network \
in Malleswaram, North Bangalore: 30 cells across 10 macro sites (3 sectors each), \
grouped under 3 DUs (DU-MLS-1/2/3) and 1 CU (CU-MLS).

Cell naming: MLS_<SITE>_<SECTOR>, e.g. MLS_RWS_01. Bands & per-cell UE limits: \
n78 3500MHz (900 UEs), n41 2500MHz (700), B40 2300MHz (300), B3 1800MHz (250). \
5G uses 64T64R, 4G 4T4R. Vendors: Nokia, Ericsson, Samsung, ZTE.

Operating guidelines:
- Always confirm before destructive actions (moving cells, applying plans, removing cells).
- Flag overloaded cells (PRB > 85%) and degraded SINR (< 5 dB).
- Summarise findings as concise bullet points or tables.
- Use the provided tools to read live state and act; never fabricate KPIs.
"""

app = FastAPI(title="RAN Orchestrator")

_gemini_sessions = {}   # session_id -> list[types.Content]
_claude_sessions = {}   # session_id -> list[{role, content}]


# --------------------------------------------------------------------------
# dynamic context
# --------------------------------------------------------------------------
def build_network_context():
    try:
        net = httpx.get(f"{CONTROLLER_URL}/network", timeout=15).json()
    except httpx.HTTPError:
        return "\n[Warning] Controller unreachable — live network snapshot unavailable.\n"
    lines = ["\nLive network snapshot:"]
    for cid, c in sorted(net.get("cells", {}).items()):
        k = c.get("kpi", {})
        lines.append(
            f"{cid} ({c.get('area','?')}) -> DU={c.get('du_id')} | "
            f"UEs={k.get('connected_ues','-')} | PRB={k.get('prb_dl_pct','-')}% | "
            f"SINR={k.get('sinr_db','-')}dB | Power={k.get('power_w','-')}W"
        )
    return "\n".join(lines) + "\n"


def _sanitise(obj):
    """Ensure tool results are JSON-serialisable."""
    return json.loads(json.dumps(obj, default=str))


# --------------------------------------------------------------------------
# Gemini backend
# --------------------------------------------------------------------------
def _gemini_client():
    from google import genai
    return genai.Client(api_key=GOOGLE_API_KEY)


def chat_turn_gemini(message, session_id):
    from google.genai import types

    client = _gemini_client()
    history = _gemini_sessions.setdefault(session_id, [])
    system = SYSTEM_PROMPT + build_network_context()
    history.append(types.Content(role="user", parts=[types.Part(text=message)]))

    config = types.GenerateContentConfig(
        system_instruction=system,
        tools=[types.Tool(function_declarations=T._clean_params())],
    )

    while True:
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=history, config=config)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if any(s in msg for s in ("429", "quota", "ResourceExhausted")):
                yield f"\n\n[Error] LLM quota/rate-limit: {msg}"
            else:
                yield f"\n\n[Error] {msg}"
            return

        cand = resp.candidates[0]
        parts = cand.content.parts or []
        history.append(cand.content)

        calls = []
        for p in parts:
            if getattr(p, "text", None):
                yield p.text
            if getattr(p, "function_call", None):
                calls.append(p.function_call)

        if not calls:
            return

        fr_parts = []
        for fc in calls:
            name = fc.name
            args = dict(fc.args or {})
            yield f"\n*[calling tool: {name}...]*\n"
            try:
                result = T.TOOL_MAP[name](args)
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}
            fr_parts.append(types.Part.from_function_response(
                name=name, response={"result": _sanitise(result)}))
        history.append(types.Content(role="user", parts=fr_parts))


# --------------------------------------------------------------------------
# Claude CLI backend
# --------------------------------------------------------------------------
def _claude_call(prompt):
    proc = subprocess.run(
        [CLAUDE_CLI_PATH, "-p", "--model", ANTHROPIC_MODEL_NAME],
        input=prompt, capture_output=True, text=True, timeout=120)
    return proc.stdout.strip() or proc.stderr.strip()


def chat_turn_claude(message, session_id):
    history = _claude_sessions.setdefault(session_id, [])
    history.append({"role": "user", "content": message})
    system = SYSTEM_PROMPT + build_network_context()
    convo = system + "\n\n" + "\n".join(
        f"{h['role']}: {h['content']}" for h in history)
    try:
        out = _claude_call(convo)
    except Exception as e:  # noqa: BLE001
        yield f"\n\n[Error] {e}"
        return
    history.append({"role": "assistant", "content": out})
    yield out


def chat_turn(message, session_id):
    if USE_CLAUDE:
        yield from chat_turn_claude(message, session_id)
    else:
        yield from chat_turn_gemini(message, session_id)


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


@app.get("/health")
def health():
    return {"status": "ok", "model": ACTIVE_MODEL}


@app.get("/tools")
def list_tools():
    return [{"name": t["name"], "description": t["description"]} for t in T.TOOL_SCHEMAS]


@app.post("/chat")
def chat(req: ChatRequest):
    return StreamingResponse(chat_turn(req.message, req.session_id),
                             media_type="text/plain")


@app.get("/history")
def get_history(session_id: str = "default"):
    if USE_CLAUDE:
        return _claude_sessions.get(session_id, [])
    out = []
    for content in _gemini_sessions.get(session_id, []):
        role = "assistant" if content.role == "model" else "user"
        text_bits = []
        for p in (content.parts or []):
            if getattr(p, "text", None):
                text_bits.append(p.text)
            elif getattr(p, "function_call", None):
                text_bits.append(f"[Calling {p.function_call.name}]")
            elif getattr(p, "function_response", None):
                text_bits.append(f"[Tool result: {p.function_response.name}]")
        out.append({"role": role, "content": " ".join(text_bits)})
    return out


@app.delete("/history")
def clear_history(session_id: str = "default"):
    _gemini_sessions.pop(session_id, None)
    _claude_sessions.pop(session_id, None)
    return {"status": "cleared", "session_id": session_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)
