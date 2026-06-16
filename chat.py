"""Operator CLI client (spec §6.6).

A standalone terminal REPL talking to the Orchestrator REST API. No LLM logic — a
pure UI layer over pure-stdlib urllib.

Usage:
  py chat.py                                # localhost:8082, session "default"
  py chat.py --url http://remote-host:8082  # remote orchestrator
  py chat.py --session ops-team             # named (isolated) session
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

COMMANDS = {
    "/status": "What is the current status of all cells, DUs, and CUs? Summarise in a table.",
    "/alerts": "Show me all recent KPI alerts from the last 60 minutes.",
    "/cells": "List all cells with their current connected UEs, PRB utilisation, and DU assignment.",
    "/plan": "Generate a network plan for Malleswaram with default parameters and show me a summary.",
}


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def _delete(url):
    req = urllib.request.Request(url, method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def chat(base, message, session):
    body = json.dumps({"message": message, "session_id": session}).encode()
    req = urllib.request.Request(f"{base}/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        # synchronous: read the full streamed body, print as it arrives
        while True:
            chunk = r.read(1024)
            if not chunk:
                break
            sys.stdout.write(chunk.decode(errors="replace"))
            sys.stdout.flush()
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8082")
    ap.add_argument("--session", default="default")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    # startup banner
    try:
        h = _get(f"{base}/health")
        print(f"== RAN Orchestrator CLI ==  model={h.get('model','?')}  url={base}  session={args.session}")
    except urllib.error.URLError as e:
        print(f"[warn] orchestrator unreachable at {base}: {e}. Continuing anyway.")

    print("Type a message, or /status /alerts /cells /plan /history /clear /tools, quit to exit.")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            break
        if line == "/history":
            for turn in _get(f"{base}/history?session_id={urllib.parse.quote(args.session)}"):
                print(f"  {turn['role']}: {turn['content'][:200]}")
            continue
        if line == "/clear":
            print(_delete(f"{base}/history?session_id={urllib.parse.quote(args.session)}"))
            continue
        if line == "/tools":
            for t in _get(f"{base}/tools"):
                print(f"  {t['name']}: {t['description']}")
            continue
        msg = COMMANDS.get(line, line)
        try:
            chat(base, msg, args.session)
        except urllib.error.URLError as e:
            print(f"[error] {e}")


if __name__ == "__main__":
    main()
