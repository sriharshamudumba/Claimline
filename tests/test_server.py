from fastapi.testclient import TestClient

import app.main as main

client = TestClient(main.app)


def _reset(code="", per_ip=6, per_day=60, key=None):
    main.DEMO_ACCESS_CODE, main.CALLS_PER_IP_PER_HOUR, main.CALLS_PER_DAY = code, per_ip, per_day
    main.OPENAI_API_KEY = key
    main._IP_CALLS.clear()
    main._DAY_CALLS.clear()


def test_access_code_blocks_live_calls_before_any_openai_call():
    _reset(code="corgi-demo")
    assert client.post("/session").status_code == 401
    assert client.post("/session", headers={"X-Access-Code": "wrong"}).status_code == 401
    # right code passes the gate (then stops at the missing key, never reaching OpenAI)
    assert client.post("/session", headers={"X-Access-Code": "corgi-demo"}).status_code == 500


def test_per_ip_rate_limit():
    _reset(per_ip=2)
    codes = [client.post("/session").status_code for _ in range(3)]
    assert codes == [500, 500, 429]


def test_daily_cap():
    _reset(per_day=1)
    assert client.post("/session").status_code == 500
    assert client.post("/session", headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 429


def test_replay_needs_no_code_and_config_reports_it():
    _reset(code="corgi-demo")
    assert client.get("/api/config").json()["access_code_required"] is True
    assert client.post("/api/demo/wire_fraud").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}


def test_sessions_are_bounded():
    _reset()
    main.MAX_SESSIONS = 5
    for _ in range(12):
        client.post("/api/demo/wire_fraud")
    assert len(main.SESSIONS) <= 5
    main.MAX_SESSIONS = 500


def test_config_exposes_filler_threshold():
    _reset()
    assert client.get("/api/config").json()["filler_ms"] == main.FILLER_MS


def test_filler_recorder_helpers():
    import array
    import importlib.util
    spec = importlib.util.spec_from_file_location("mf", main.STATIC_DIR.parent / "scripts" / "make_fillers.py")
    mf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mf)
    loud = array.array("h", [0] * 2400 + [9000] * 4800 + [0] * 2400).tobytes()
    trimmed = mf.trim_silence(loud)
    assert len(trimmed) // 2 < 2400 + 4800 + 2400 and len(trimmed) // 2 >= 4800
    assert mf.trim_silence(b"\x00\x00" * 1000) == b""
    assert mf.looks_right("Mm-hmm.", "Mhm.")
    assert not mf.looks_right("Okay.", "Okay, so tell me what happened next with the truck.")


def test_agent_turn_guardrail_endpoint():
    sid = client.post("/api/demo/guardrails_check").json()["session_id"]
    r = client.post("/api/guardrails/agent_turn", json={"session_id": sid, "text": "Don't worry, you're covered."}).json()
    assert r["violations"][0]["code"] == "coverage_promise"
    assert r["correction"].startswith("GUARDRAIL")
    assert r["claim"]["guardrails"]["agent_turns_checked"] == 1
    ok = client.post("/api/guardrails/agent_turn", json={"session_id": sid, "text": "An adjuster makes that call."}).json()
    assert ok["violations"] == [] and ok["correction"] is None


def test_session_asks_for_minimal_reasoning_and_fast_turns(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"value": "ek_test"}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            sent.update(json)
            return Resp()

    _reset(key="sk-test")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    assert client.post("/session").status_code == 200
    assert sent["session"]["reasoning"] == {"effort": "minimal"}
    assert sent["session"]["audio"]["input"]["turn_detection"] == {"type": "semantic_vad", "eagerness": "high"}
