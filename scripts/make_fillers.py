#!/usr/bin/env python3
"""Record filler clips ("mm-hmm", "okay", "let me pull that up") in the agent's own voice.

Uses the same Realtime model and voice as the live agent, so the filler sounds like the
agent, not a different speaker. Run once, then commit static/fillers/ so deploys include it.

    python scripts/make_fillers.py            # skips clips that already exist
    python scripts/make_fillers.py --force    # re-record everything

Cost: a few cents in total.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import json
import os
import re
import sys
import wave
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "fillers"
load_dotenv(ROOT / ".env", override=True)

RATE = 24_000  # Realtime PCM output: 24 kHz, 16-bit mono

CLIPS: dict[str, list[str]] = {
    "ack": ["Mm-hmm.", "Okay.", "Right.", "Mm, okay.", "Uh-huh.", "Got it."],
    "lookup": ["Let me pull that up.", "One moment.", "Okay, let me check that."],
}

ACTOR = (
    "You are a voice actor recording short filler sounds for a warm, calm, experienced insurance claims "
    "representative on a phone call. When given a line, say exactly that line once, softly and naturally, "
    "like a person who is listening and about to respond. Say nothing before or after it."
)


def words(s: str) -> list[str]:
    return re.findall(r"[a-z]+", s.lower())


def looks_right(target: str, transcript: str) -> bool:
    """Loose check: filler transcripts vary ('Mhm' vs 'Mm-hmm'), but extra sentences mean a bad take."""
    t, got = words(target), words(transcript)
    return 0 < len(got) <= len(t) + 2


def trim_silence(pcm: bytes, threshold: int = 500, pad_ms: int = 40) -> bytes:
    """Cut leading/trailing near-silence so the filler starts instantly."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    loud = [i for i, s in enumerate(samples) if abs(s) > threshold]
    if not loud:
        return b""
    pad = RATE * pad_ms // 1000
    start, end = max(0, loud[0] - pad), min(len(samples), loud[-1] + pad)
    out = samples[start:end]
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


async def record(line: str, model: str, voice: str, key: str, url: str | None) -> tuple[bytes, str]:
    import websockets

    ws_url = url or f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with websockets.connect(ws_url, additional_headers=headers, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime",
            "instructions": ACTOR,
            "output_modalities": ["audio"],
            "audio": {"output": {"voice": voice, "format": {"type": "audio/pcm", "rate": RATE}}},
        }}))
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": f"Line: {line}"}]}}))
        await ws.send(json.dumps({"type": "response.create"}))
        pcm, transcript = bytearray(), ""
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            kind = ev.get("type", "")
            if kind == "error":
                raise RuntimeError(ev.get("error"))
            if kind in ("response.output_audio.delta", "response.audio.delta"):
                pcm += base64.b64decode(ev["delta"])
            elif kind in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                transcript = ev.get("transcript", "")
            elif kind == "response.done":
                return bytes(pcm), transcript


async def main() -> int:
    ap = argparse.ArgumentParser(description="Record filler clips in the agent's voice")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--model", default=os.getenv("REALTIME_MODEL", "gpt-realtime"))
    ap.add_argument("--voice", default=os.getenv("REALTIME_VOICE", "marin"))
    ap.add_argument("--realtime-url", help=argparse.SUPPRESS)  # offline testing
    args = ap.parse_args()
    key = os.getenv("OPENAI_API_KEY", "")
    if not key and not args.realtime_url:
        print("OPENAI_API_KEY missing in .env")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"model": args.model, "voice": args.voice, "clips": {}}
    for kind, lines in CLIPS.items():
        manifest["clips"][kind] = []
        for i, line in enumerate(lines, 1):
            name = f"{kind}_{i}.wav"
            path = OUT / name
            if path.exists() and not args.force:
                print(f"  skip  {name} (exists)")
                manifest["clips"][kind].append({"file": name, "text": line})
                continue
            for attempt in range(1, 4):
                pcm, transcript = await record(line, args.model, args.voice, key, args.realtime_url)
                clip = trim_silence(pcm)
                if clip and looks_right(line, transcript):
                    write_wav(path, clip)
                    ms = len(clip) * 1000 // (RATE * 2)
                    print(f"  ok    {name}  '{line}' -> heard '{transcript.strip()}'  {ms} ms")
                    manifest["clips"][kind].append({"file": name, "text": line, "ms": ms})
                    break
                print(f"  retry {name} (attempt {attempt}: heard '{transcript.strip()}')")
            else:
                print(f"  FAIL  {name}: no clean take after 3 tries, skipping")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    total = sum(len(v) for v in manifest["clips"].values())
    print(f"\n{total} clips in {OUT.relative_to(ROOT)}. Voice '{args.voice}' on {args.model}.")
    print("Commit static/fillers/ so the deployed app has them.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
