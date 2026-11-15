#!/usr/bin/env python3
"""Voice the recorded calls: every agent and caller line in app/scenarios.json, read as written.

The agent uses the live agent's voice. Each caller gets their own voice and direction, so the
replay sounds like two people on a call. Clips go to static/replay/ with a manifest the browser
reads. Lines whose text changed since the last run are re-recorded; the rest are skipped.

    python scripts/make_replay_audio.py                 # all scenarios
    python scripts/make_replay_audio.py trucking_crash  # just one
    python scripts/make_replay_audio.py --force         # re-record everything

Needs OPENAI_API_KEY in .env. ffmpeg (sudo apt install ffmpeg) turns clips into small MP3s;
without it you get WAV files, roughly 10x larger.
Cost: well under $1 for all 8 scenarios.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_fillers import RATE, record, trim_silence, write_wav  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)
SCENARIOS = ROOT / "app" / "scenarios.json"
OUT = ROOT / "static" / "replay"

AGENT_ACTOR = (
    "You are a voice actor playing a warm, calm, experienced insurance claims representative on a phone call. "
    "You will be given one script line. Read it out loud exactly as written, word for word, once. "
    "Do not answer it, add to it, or comment on it. Speak at a natural phone pace with real warmth, "
    "the way a person who does this every day sounds. Read letters and digits one at a time when they are spaced out."
)

CALLER_ACTOR = (
    "You are a voice actor playing a caller on a phone call to their insurance company. Character: {direction} "
    "You will be given one script line. Read it out loud exactly as written, word for word, once, in character. "
    "Do not answer it, add to it, or comment on it. Sound like a real person on the phone, not an announcer. "
    "Read letters and digits one at a time when they are spaced out."
)

# Caller voice and direction per scenario. Voices are Realtime voices distinct from the agent's.
CALLERS: dict[str, tuple[str, str]] = {
    "trucking_crash": ("cedar", "Marcus Hill, a truck driver who rear-ended a car two hours ago. Shaken, tired, "
                                "a little unsteady, speaks plainly, with small pauses."),
    "ransomware": ("coral", "Priya Shah, operations lead at a startup whose servers were just encrypted. "
                            "Stressed and fast, but composed and businesslike."),
    "wire_fraud": ("ash", "Dan Wu, CFO of a startup who just learned a $186,400 wire went to a fraudster. "
                          "Tense, embarrassed, trying to stay professional."),
    "eo_ai_demand": ("coral", "Priya Shah, operations lead at a startup that received a demand letter from a customer. "
                              "Concerned but steady and precise."),
    "third_party": ("sage", "Rosa Delgado, a driver who was sideswiped by a truck this afternoon. "
                            "Shaken, her shoulder hurts, slightly guarded with the trucking company's insurer."),
    "edge_no_coverage": ("echo", "Leo Park, a young startup founder reporting an exposed data bucket. "
                                 "Calm, quick, a bit anxious about whether he is covered."),
    "claim_status": ("cedar", "Marcus Hill, a truck driver checking on an older cargo claim. Relaxed, friendly, practical."),
    "guardrails_check": ("shimmer", "Jen Okafor, safety manager at a trucking company. Brisk, a little impatient, "
                                    "testing the system, but not rude."),
    "coi_request": ("coral", "Priya Shah, operations lead who needs a certificate for a bank by a deadline. "
                             "Upbeat, busy, to the point."),
}
DEFAULT_CALLER = ("cedar", "A small business owner calling their insurer. Natural and a little rushed.")


def words(s: str) -> list[str]:
    # Join spelled-out letters ("x y z") so "XYZ" and "X Y Z" compare equal.
    s = re.sub(r"\b(?:[a-z][ .-]+)+[a-z]\b", lambda m: re.sub(r"[ .-]", "", m.group(0)), s.lower())
    return re.findall(r"[a-z]+", s)


def read_as_written(target: str, transcript: str) -> bool:
    """True when the take reads the line rather than answering or improvising on it.

    Digits are ignored (the transcript may write '186,400' or 'one eighty-six'), so short
    all-digit lines only need to stay short.
    """
    t, g = words(target), words(transcript)
    if not g:
        return False
    if len(t) < 4:
        return len(g) <= len(t) + 8
    return SequenceMatcher(None, t, g).ratio() >= 0.75 and len(g) <= len(t) * 1.3 + 3


def encode(pcm: bytes, dest_stem: Path) -> Path:
    """MP3 via ffmpeg when available, else WAV."""
    if shutil.which("ffmpeg"):
        path = dest_stem.with_suffix(".mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "-",
             "-codec:a", "libmp3lame", "-b:a", "48k", str(path)],
            input=pcm, check=True,
        )
        return path
    path = dest_stem.with_suffix(".wav")
    write_wav(path, pcm)
    return path


async def voice_line(sem, text, actor, voice, model, key, url, dest_stem, label):
    async with sem:
        for attempt in range(1, 4):
            try:
                pcm, transcript = await record(text, model, voice, key, url, instructions=actor)
            except Exception as err:  # network blips: retry, then give up on this line
                print(f"  retry {label} (attempt {attempt}: {err})")
                continue
            clip = trim_silence(pcm, pad_ms=120)
            if clip and read_as_written(text, transcript):
                path = encode(clip, dest_stem)
                ms = len(clip) * 1000 // (RATE * 2)
                print(f"  ok    {label}  {ms} ms")
                return path, ms
            print(f"  retry {label} (attempt {attempt}: heard '{transcript.strip()[:80]}')")
        print(f"  FAIL  {label}: no clean take, the replay will show this line without audio")
        return None, 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="Voice the recorded calls")
    ap.add_argument("scenarios", nargs="*", help="scenario ids (default: all)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--model", default=os.getenv("REALTIME_MODEL", "gpt-realtime"))
    ap.add_argument("--agent-voice", default=os.getenv("REALTIME_VOICE", "marin"))
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--realtime-url", help=argparse.SUPPRESS)  # offline testing
    args = ap.parse_args()
    key = os.getenv("OPENAI_API_KEY", "")
    if not key and not args.realtime_url:
        print("OPENAI_API_KEY missing in .env")
        return 1
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found, writing WAV (large). Install it with: sudo apt install ffmpeg\n")

    scenarios = json.loads(SCENARIOS.read_text())
    ids = {s["id"] for s in scenarios}
    for bad in set(args.scenarios) - ids:
        print(f"Unknown scenario '{bad}'. Known: {', '.join(sorted(ids))}")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest.setdefault("scenarios", {})
    sem = asyncio.Semaphore(max(1, args.parallel))

    for sc in scenarios:
        sid = sc["id"]
        if args.scenarios and sid not in args.scenarios:
            continue
        caller_voice, direction = CALLERS.get(sid, DEFAULT_CALLER)
        old = manifest["scenarios"].get(sid, {}).get("lines", {})
        folder = OUT / sid
        folder.mkdir(exist_ok=True)
        print(f"{sid}: agent '{args.agent_voice}', caller '{caller_voice}'")

        jobs, lines = [], {}
        for i, step in enumerate(sc["steps"]):
            who = "agent" if "agent" in step else "caller" if "caller" in step else None
            if not who:
                continue
            text = step[who]
            prev = old.get(str(i))
            if prev and prev.get("text") == text and (folder / prev["file"]).exists() and not args.force:
                lines[str(i)] = prev
                continue
            for stale in folder.glob(f"{i:02d}_*"):
                stale.unlink()
            voice = args.agent_voice if who == "agent" else caller_voice
            actor = AGENT_ACTOR if who == "agent" else CALLER_ACTOR.format(direction=direction)
            label = f"{sid}/{i:02d} {who}"
            jobs.append((i, who, text, voice, asyncio.create_task(voice_line(
                sem, text, actor, voice, args.model, key, args.realtime_url, folder / f"{i:02d}_{who}", label))))
        for i, who, text, voice, task in jobs:
            path, ms = await task
            if path:
                lines[str(i)] = {"file": path.name, "who": who, "voice": voice, "text": text, "ms": ms}
        manifest["scenarios"][sid] = {"agent_voice": args.agent_voice, "caller_voice": caller_voice,
                                      "lines": dict(sorted(lines.items(), key=lambda kv: int(kv[0])))}
        manifest_path.write_text(json.dumps(manifest, indent=2))

    total = sum(len(s["lines"]) for s in manifest["scenarios"].values())
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) / 1e6
    print(f"\n{total} clips, {size:.1f} MB in {OUT.relative_to(ROOT)}. Commit static/replay/ so the deploy has them.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
