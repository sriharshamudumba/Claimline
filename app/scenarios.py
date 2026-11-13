"""Load demo scenarios and resolve relative time tokens.

Tokens: @dt-30h -> ISO datetime, @d-2h -> date, @t-2h -> HH:MM, all relative to `now`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

_PATH = Path(__file__).with_name("scenarios.json")
_TOKEN = re.compile(r"@(dt|d|t)([+-]\d+(?:\.\d+)?)h")


def _sub(text: str, now: datetime) -> str:
    def repl(m: re.Match) -> str:
        dt = now + timedelta(hours=float(m.group(2)))
        return {"dt": dt.isoformat(timespec="minutes"), "d": dt.date().isoformat(), "t": dt.strftime("%H:%M")}[m.group(1)]

    return _TOKEN.sub(repl, text)


def _resolve(obj, now: datetime):
    if isinstance(obj, str):
        return _sub(obj, now)
    if isinstance(obj, list):
        return [_resolve(x, now) for x in obj]
    if isinstance(obj, dict):
        return {k: _resolve(v, now) for k, v in obj.items()}
    return obj


def load(now: datetime) -> list[dict]:
    return _resolve(json.loads(_PATH.read_text()), now)


def get(scenario_id: str, now: datetime) -> dict | None:
    return next((s for s in load(now) if s["id"] == scenario_id), None)
