"""FastAPI server: mints Realtime client secrets and executes tool calls."""

from __future__ import annotations

import hmac
import os
import time
import uuid
from collections import deque
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from datetime import datetime

from . import scenarios
from .claim_engine import ClaimSession
from .prompt import TOOLS, build_instructions

load_dotenv(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime")
VOICE = os.getenv("REALTIME_VOICE", "marin")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "").strip()  # optional: only feeds the caller-side transcript
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# --- Public-demo guardrails (live calls cost money; replay is free) ---
DEMO_ACCESS_CODE = os.getenv("DEMO_ACCESS_CODE", "")          # empty = no code required (local dev)
CALLS_PER_IP_PER_HOUR = int(os.getenv("CALLS_PER_IP_PER_HOUR", "6"))
CALLS_PER_DAY = int(os.getenv("CALLS_PER_DAY", "60"))
MAX_CALL_MINUTES = int(os.getenv("MAX_CALL_MINUTES", "8"))
FILLER_MS = int(os.getenv("FILLER_MS", "500"))  # play a filler if the agent hasn't spoken by then; 0 = off
SESSION_TTL_SECONDS = 3 * 3600
MAX_SESSIONS = 500

app = FastAPI(title="Claimline")  # FNOL intake and triage demo
print(f"[claimline] realtime model={MODEL!r} key=...{(OPENAI_API_KEY or '')[-4:]} "
      f"transcribe={TRANSCRIBE_MODEL or 'off'} access_code={'on' if DEMO_ACCESS_CODE else 'off'}", flush=True)
SESSIONS: dict[str, ClaimSession] = {}
_CREATED: dict[str, float] = {}
_IP_CALLS: dict[str, deque] = {}
_DAY_CALLS: deque = deque()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def _check_live_call_allowed(request: Request) -> None:
    """Access code, then per-IP and daily limits. Runs before any OpenAI call."""
    if DEMO_ACCESS_CODE:
        given = request.headers.get("x-access-code", "")
        if not hmac.compare_digest(given.encode(), DEMO_ACCESS_CODE.encode()):
            raise HTTPException(401, "Access code required for live calls. Replay works without one.")
    now = time.time()
    while _DAY_CALLS and now - _DAY_CALLS[0] > 86400:
        _DAY_CALLS.popleft()
    if len(_DAY_CALLS) >= CALLS_PER_DAY:
        raise HTTPException(429, "Daily live-call limit reached for this demo. Replay still works.")
    calls = _IP_CALLS.setdefault(_client_ip(request), deque())
    while calls and now - calls[0] > 3600:
        calls.popleft()
    if len(calls) >= CALLS_PER_IP_PER_HOUR:
        raise HTTPException(429, "Too many live calls from this network in the last hour. Try again later.")
    calls.append(now)
    _DAY_CALLS.append(now)


def _new_session() -> str:
    """Create a claim session and evict old ones so memory stays bounded."""
    now = time.time()
    for sid in [s for s, t in _CREATED.items() if now - t > SESSION_TTL_SECONDS]:
        SESSIONS.pop(sid, None)
        _CREATED.pop(sid, None)
    while len(SESSIONS) >= MAX_SESSIONS:
        oldest = min(_CREATED, key=_CREATED.get)
        SESSIONS.pop(oldest, None)
        _CREATED.pop(oldest, None)
    session_id = uuid.uuid4().hex[:12]
    SESSIONS[session_id] = ClaimSession(session_id)
    _CREATED[session_id] = now
    return session_id


@app.get("/api/config")
def config() -> dict:
    return {"access_code_required": bool(DEMO_ACCESS_CODE), "max_call_minutes": MAX_CALL_MINUTES, "filler_ms": FILLER_MS}


@app.post("/session")
async def create_session(request: Request) -> dict:
    """Create a claim session and a short-lived Realtime key for the browser.

    The real API key never leaves the server. The browser only gets an
    ephemeral client secret scoped to one session config.
    """
    _check_live_call_allowed(request)
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY is not set")

    session_id = _new_session()

    payload = {
        "session": {
            "type": "realtime",
            "model": MODEL,
            "instructions": build_instructions(),
            "tools": TOOLS,
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "noise_reduction": {"type": "near_field"},
                    "turn_detection": {"type": "semantic_vad"},
                },
                "output": {"voice": VOICE},
            },
        }
    }
    if TRANSCRIBE_MODEL:
        payload["session"]["audio"]["input"]["transcription"] = {"model": TRANSCRIBE_MODEL}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json=payload,
        )
    if r.status_code >= 400:
        raise HTTPException(502, f"OpenAI error {r.status_code}: {r.text}")

    data = r.json()
    secret = data.get("value") or (data.get("client_secret") or {}).get("value")
    if not secret:
        raise HTTPException(502, f"No client secret in response: {data}")
    return {"session_id": session_id, "client_secret": secret, "model": MODEL, "max_call_minutes": MAX_CALL_MINUTES}


class ToolCall(BaseModel):
    session_id: str
    args: dict = Field(default_factory=dict)


def _get(session_id: str) -> ClaimSession:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    return session


@app.post("/api/tools/{name}")
def run_tool(name: str, call: ToolCall) -> dict:
    session = _get(call.session_id)
    result = session.dispatch(name, call.args)
    return {"tool_result": result, "claim": session.snapshot()}


class AgentTurn(BaseModel):
    session_id: str
    text: str = Field(max_length=4000)


@app.post("/api/guardrails/agent_turn")
def agent_turn(turn: AgentTurn) -> dict:
    """Output guardrail: the browser posts each finished agent turn; violations come back with a correction."""
    session = _get(turn.session_id)
    result = session.check_agent_turn(turn.text)
    return {**result, "claim": session.snapshot()}


@app.get("/api/claim/{session_id}")
def get_claim(session_id: str) -> dict:
    return _get(session_id).snapshot()


@app.get("/api/scenarios")
def list_scenarios() -> list[dict]:
    return [{"id": x["id"], "title": x["title"]} for x in scenarios.load(datetime.now())]


@app.post("/api/demo/{scenario_id}")
def start_demo(scenario_id: str) -> dict:
    """Scripted replay: no OpenAI key needed. Tool calls still run through the real engine."""
    scn = scenarios.get(scenario_id, datetime.now())
    if scn is None:
        raise HTTPException(404, "unknown scenario")
    session_id = _new_session()
    return {"session_id": session_id, "title": scn["title"], "steps": scn["steps"]}


@app.get("/api/events/{session_id}")
def get_events(session_id: str) -> list[dict]:
    return _get(session_id).events


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
