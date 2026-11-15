#!/usr/bin/env python3
"""Pre-deploy check for Claimline.

Runs three stages and prints PASS/FAIL for each check:
  1. Server: health, UI, every replay scenario through the API, access-code gate.
  2. OpenAI session: the server can mint a Realtime client secret with your key and model.
  3. Realtime model: a full text-mode call with the real model and real tools.
     A scripted caller (Marcus, trucking crash) talks to the agent; the script checks that
     the agent asks how he is, matches the policy, triggers DOT testing, and submits.

Usage (server must be running in another terminal):
    python scripts/predeploy.py --url http://localhost:8001
    python scripts/predeploy.py --skip-openai          # stage 1 only, free

Stage 3 uses the Realtime API in text mode, so it costs a few cents at most.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    return ok


# ------------------------------------------------------------------ stage 1

def stage_server(url: str, code: str) -> bool:
    from app import scenarios

    print(f"\n[1/3] Server and guardrails at {url}")
    with httpx.Client(base_url=url, timeout=20) as c:
        try:
            r = c.get("/healthz")
        except httpx.ConnectError:
            check("server reachable", False, "start it first: python -m uvicorn app.main:app --port 8001")
            return False
        check("health check", r.status_code == 200)
        page = c.get("/")
        check("UI loads", page.status_code == 200 and "Claimline" in page.text)

        expect = {s["id"]: s["expect"] for s in scenarios.load(datetime.now())}
        for sc in c.get("/api/scenarios").json():
            demo = c.post(f"/api/demo/{sc['id']}").json()
            errors, snap = [], None
            for step in demo["steps"]:
                if "tool" not in step:
                    continue
                body = c.post(f"/api/tools/{step['tool']}", json={"session_id": demo["session_id"], "args": step["args"]}).json()
                tr = body["tool_result"]
                if "error" in tr or tr.get("rejected"):
                    errors.append(f"{step['tool']}: {tr.get('error') or tr.get('rejected')}")
                snap = body["claim"]
            e = expect[sc["id"]]
            routed = snap["routing"]["queue"] if snap.get("routing") else snap.get("call_reason")
            ok = (not errors and snap["status"] == e["status"]
                  and ("queue" not in e or (snap.get("routing") or {}).get("queue") == e["queue"])
                  and ("call_reason" not in e or snap.get("call_reason") == e["call_reason"]))
            check(f"replay {sc['id']}", ok, "; ".join(errors) or f"{snap['status']} -> {routed}")

        cfg = c.get("/api/config").json()
        if cfg["access_code_required"]:
            check("live calls blocked without access code", c.post("/session").status_code == 401)
            if not code:
                print("  NOTE  Server wants an access code. Pass --code or set DEMO_ACCESS_CODE in .env for stage 2.")
        else:
            print("  NOTE  DEMO_ACCESS_CODE is empty. Fine locally; set it before deploying publicly.")
        print(f"  INFO  live calls auto-end after {cfg['max_call_minutes']} min")
    return True


# ------------------------------------------------------------------ stage 2

def stage_session(url: str, code: str) -> bool:
    print("\n[2/3] OpenAI session minting (your key + model, through the server)")
    with httpx.Client(base_url=url, timeout=30) as c:
        r = c.post("/session", headers={"X-Access-Code": code})
    if r.status_code != 200:
        detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
        check("server minted a realtime session", False, f"HTTP {r.status_code}: {str(detail)[:400]}")
        hints = {
            401: "Wrong or missing access code. Pass --code.",
            429: "Rate limit hit. Restart the server to reset, or raise CALLS_PER_IP_PER_HOUR.",
            500: "OPENAI_API_KEY not loaded. Is .env in the project folder? Restart the server after editing it.",
            502: "OpenAI rejected the request. Read the error above: usually the model name or the session payload shape.",
        }
        print(f"  HINT  {hints.get(r.status_code, 'See the error above.')}")
        return False
    body = r.json()
    check("server minted a realtime session", bool(body.get("client_secret")), f"model={body.get('model')}")
    return True


# ------------------------------------------------------------------ stage 3

CALLER_LINES = [
    "I need to report an accident. I rear-ended a car on I-80 a couple of hours ago.",
    "I'm okay, not hurt. Just shaken up.",
    "Marcus Hill. I'm the driver for XYZ Freight.",
    "I rear-ended a sedan on I-80 westbound near exit 15 in Reno, Nevada, about two hours ago. Traffic stopped suddenly and "
    "I couldn't stop in time. One other car. The other driver was taken by ambulance with neck pain. Nobody died.",
    "The policy number is C T 2 0 4 1 7.",
    "Yes, that's right.",
    "I was in the Cascadia, unit T1, under dispatch hauling produce. The cargo is fine, no hazmat.",
    "Both vehicles got towed. My truck is at TUV Towing in Sparks. The trooper gave me a citation for following too close.",
    "Police report number is N H P 26 118842. And yes, the truck has a dashcam.",
    "Yes, that's all correct.",
    "Yes, please go ahead and submit it.",
    "Yes, submit it.",
]


def _texts(response: dict) -> list[str]:
    out = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            text = part.get("text") or part.get("transcript")
            if text:
                out.append(text)
    return out


async def stage_realtime(model: str, ws_url: str | None, verbose: bool) -> bool:
    import websockets

    from app.claim_engine import ClaimSession
    from app.prompt import TOOLS, build_instructions

    print(f"\n[3/3] Realtime model test: text-mode call with {model}")
    key = os.getenv("OPENAI_API_KEY")
    if not key and not ws_url:
        check("OPENAI_API_KEY in .env", False)
        return False

    session = ClaimSession("predeploy")
    agent_lines: list[str] = []
    tool_calls: list[tuple[str, dict, dict]] = []
    url = ws_url or f"wss://api.openai.com/v1/realtime?model={model}"

    async def recv(ws) -> dict:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=60))

    async def agent_turn(ws) -> None:
        """Ask for a response; run tool calls until the agent finishes a turn without tools."""
        await ws.send(json.dumps({"type": "response.create"}))
        for _ in range(12):  # safety cap on tool loops per turn
            while True:
                ev = await recv(ws)
                if ev["type"] == "error":
                    raise RuntimeError(f"API error: {ev.get('error')}")
                if ev["type"] == "response.done":
                    break
            resp = ev["response"]
            if resp.get("status") == "failed":
                raise RuntimeError(f"response failed: {resp.get('status_details')}")
            for text in _texts(resp):
                agent_lines.append(text)
                print(f"    Agent : {text}")
            calls = [o for o in resp.get("output", []) if o.get("type") == "function_call"]
            if not calls:
                return
            for call in calls:
                try:
                    args = json.loads(call.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = session.dispatch(call["name"], args)
                tool_calls.append((call["name"], args, result))
                bad = result.get("error") or result.get("rejected")
                if verbose or bad:
                    print(f"    tool  : {call['name']}({json.dumps(args)[:160]}) -> {'ERR ' + str(bad)[:160] if bad else 'ok'}")
                else:
                    print(f"    tool  : {call['name']} ok")
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(result)}}))
            await ws.send(json.dumps({"type": "response.create"}))

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
            first = await recv(ws)
            if first["type"] == "error":
                raise RuntimeError(f"API error on connect: {first.get('error')}")
            await ws.send(json.dumps({"type": "session.update", "session": {
                "type": "realtime",
                "instructions": build_instructions(),
                "tools": TOOLS,
                "tool_choice": "auto",
                "output_modalities": ["text"],
            }}))
            await agent_turn(ws)  # agent opens the call
            for line in CALLER_LINES:
                if session.status != "intake":
                    break
                print(f"    Caller: {line}")
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                    "type": "message", "role": "user", "content": [{"type": "input_text", "text": line}]}}))
                await agent_turn(ws)
    except Exception as exc:  # noqa: BLE001 - surface anything to the user
        check("realtime conversation ran", False, str(exc)[:500])
        print("  HINT  If this mentions an unknown parameter or event, the Realtime API shape changed. Paste this error to Claude.")
        return False

    check("realtime conversation ran", True, f"{len(agent_lines)} agent turns, {len(tool_calls)} tool calls")
    snap = session.snapshot()
    opener = (agent_lines[0] if agent_lines else "").lower()
    check("agent opened by asking why they're calling", any(w in opener for w in ("help", "calling about", "what can")), opener[:90])
    reasons = [a.get("reason") for n, a, _ in tool_calls if n == "set_call_reason"]
    check("agent classified the call as a new claim", "new_claim" in reasons, str(reasons))
    check("caller wellbeing recorded", snap["caller_wellbeing"]["ok"] is not None)
    check("policy matched", bool(snap["policy"]))
    check("claim opened as trucking", snap["line"] == "trucking", str(snap["line"]))
    dot = snap.get("dot_testing") or {}
    check("DOT post-accident testing triggered", dot.get("required") is True)
    spoke_dot = any("test" in t.lower() for t in agent_lines)
    check("agent told the caller about the test window", spoke_dot)
    check("claim submitted", snap["status"].startswith("submitted"), f"{snap['status']}, missing: {snap['missing_required']}")
    rejected = [(n, r.get("error") or r.get("rejected")) for n, _, r in tool_calls if r.get("error") or r.get("rejected")]
    if rejected:
        print(f"  WARN  {len(rejected)} tool call(s) rejected and retried (normal in small numbers): {rejected[:3]}")
    if snap["status"].startswith("submitted"):
        print(f"  INFO  claim {snap['claim_number']} -> {snap['routing']['queue']}, severity {snap['severity']}/5")
    return True


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description="Claimline pre-deploy check")
    ap.add_argument("--url", default="http://localhost:8001", help="running server, local or deployed")
    ap.add_argument("--code", default=os.getenv("DEMO_ACCESS_CODE", ""), help="demo access code, if the server requires one")
    ap.add_argument("--model", default=os.getenv("REALTIME_MODEL", "gpt-realtime"))
    ap.add_argument("--skip-openai", action="store_true", help="run stage 1 only (no API cost)")
    ap.add_argument("--verbose", action="store_true", help="print full tool arguments in stage 3")
    ap.add_argument("--realtime-url", help=argparse.SUPPRESS)  # for offline testing against a mock
    args = ap.parse_args()

    if stage_server(args.url, args.code) and not args.skip_openai:
        stage_session(args.url, args.code)
        asyncio.run(stage_realtime(args.model, args.realtime_url, args.verbose))

    failed = [n for n, ok in RESULTS if not ok]
    print("\n" + ("ALL CHECKS PASSED. Ready to deploy." if not failed else f"{len(failed)} CHECK(S) FAILED: " + ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
