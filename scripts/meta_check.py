"""Check that the Meta Model API works for Talk: the key, the language model on a few caller sentences, and speech recognition on recordings.

The key and address come from .env (LLM_API_KEY, LLM_URL with LLM_PROVIDER=meta; put them there yourself, and never commit the file). A
META_API_KEY in the environment wins over the file. The script never prints the key.

    python -m scripts.meta_check                          # the key, the model list, the language model (JSON turns, rerank), time per call
    python -m scripts.meta_check --audio call.wav         # also reads one recording both ways and prints what was heard
    python -m scripts.meta_check --dir recordings/en      # name.wav + name.txt (the true words): word error rate of both ways, like asr_gate

Two ways to hear: "transcribe" is Muse Voice Transcribe (made for speech, 25 languages, keyword hints), "spark" is Muse Spark 1.2 given the audio
itself (it takes audio, and Meta recommends 1.2 for it). The recordings are sent as the phone gave them (8 kHz) and resampled to 16 kHz first.
"""

import argparse
import asyncio
import base64
import json
import os
import statistics
import time
from pathlib import Path

import httpx

from sofa.audio import to_16k_mono
from sofa.config import get_settings
from sofa.clients.llm import LLMClient, TurnJSON
from sofa.textutil import wer

BASE = "https://api.meta.ai"
SYSTEM = ("You are the phone assistant of a Lagos pharmacy and health line. A caller said the sentence below. Fill in the JSON for this turn: "
          "the intent, any products, and one short sentence to say back.")
SENTENCES = ["I want two packs of paracetamol", "Where is the nearest clinic in Surulere", "Do you have a pharmacy open tonight", "Yes please go ahead"]
CHOICES = ["Paracetamol 500mg Tablets", "Paracetamol Syrup 120ml", "Vitamin C 1000mg"]


def timed(label: str, start: float, note: str = "") -> None:
    print(f"  {label}: {time.perf_counter() - start:.2f} s {note}".rstrip())


async def check_model(key: str, model: str) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(f"{BASE}/v1/models", headers={"Authorization": f"Bearer {key}"})
    if resp.status_code != 200:
        raise SystemExit(f"the key was not accepted: HTTP {resp.status_code}: {resp.text[:200]}")
    ids = sorted(m.get("id", "?") for m in resp.json().get("data", []))
    print(f"key accepted; {len(ids)} models: {', '.join(ids)}")
    if model not in ids:
        print(f"  note: {model} is not in the list; pass --model with one that is")

    llm = LLMClient(BASE, model, key, provider="meta")
    print(f"\nlanguage model {model}, JSON turns:")
    for said in SENTENCES:
        start = time.perf_counter()
        turn, raw = await llm.parse_turn(SYSTEM, said)
        timed(f'"{said}"', start, f"-> {'REJECTED ' + str(raw) if 'error' in raw else turn.intent + ' | ' + (turn.say or '')[:70]}")
    start = time.perf_counter()
    pick = await llm.rerank("paracetamol tablets", CHOICES)
    timed("rerank 'paracetamol tablets'", start, f"-> {pick} (expected 1)")


async def hear_transcribe(key: str, wav: bytes, keywords: list[str]) -> str:
    request = {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV", "mode": "PUSH_TO_TALK", "languageBias": ["English"]}
    if keywords:
        request["keywords"] = keywords
    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.post(f"{BASE}/v1/asr/transcribe", headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                               files={"request": (None, json.dumps(request), "application/json"), "audio": ("turn.wav", wav, "audio/wav")})
    resp.raise_for_status()
    return resp.json().get("transcript", "")


async def hear_spark(key: str, model: str, wav: bytes) -> str:
    payload = {"model": model, "max_tokens": 4000, "reasoning_effort": "minimal", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Transcribe this phone call audio exactly as spoken. Return only the transcript."},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}},
    ]}]}
    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.post(f"{BASE}/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=payload)
    resp.raise_for_status()
    return (resp.json()["choices"][0]["message"]["content"] or "").strip()


async def hear(key: str, spark_model: str, path: Path, keywords: list[str]) -> dict:
    wav = to_16k_mono(path.read_bytes())
    out = {}
    for name, call in (("transcribe", lambda: hear_transcribe(key, wav, keywords)), ("spark", lambda: hear_spark(key, spark_model, wav))):
        start = time.perf_counter()
        try:
            out[name] = (await call(), time.perf_counter() - start)
        except httpx.HTTPError as exc:
            out[name] = (f"FAILED: {exc}", time.perf_counter() - start)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="the language model (default: LLM_MODEL from .env; Standard mode, never a -contributor model)")
    ap.add_argument("--audio")
    ap.add_argument("--dir")
    ap.add_argument("--keywords", default="", help="comma-separated words to listen for, e.g. names of drugs and places")
    args = ap.parse_args()
    args.model = args.model or get_settings().llm_model
    global BASE
    settings = get_settings()
    key = os.environ.get("META_API_KEY") or (settings.llm_api_key if settings.llm_provider == "meta" else "")
    if settings.llm_provider == "meta" and settings.llm_url.startswith("http"):
        BASE = settings.llm_url.rstrip("/")
    if not key:
        raise SystemExit("no key found: put LLM_PROVIDER=meta and LLM_API_KEY=<your key> in .env first")
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    await check_model(key, args.model)

    files = [Path(args.audio)] if args.audio else sorted(Path(args.dir).glob("*.wav")) if args.dir else []
    rows = {"transcribe": [], "spark": []}
    for path in files:
        heard = await hear(key, args.model, path, keywords)
        ref_path = path.with_suffix(".txt")
        ref = ref_path.read_text(encoding="utf-8").strip() if ref_path.exists() else ""
        print(f"\n{path.name}" + (f"\n  said:       {ref}" if ref else ""))
        for name, (text, secs) in heard.items():
            score = ""
            if ref and not text.startswith("FAILED"):
                rows[name].append(wer(ref, text))
                score = f"  WER {rows[name][-1]:.2f}"
            print(f"  {name:<10}  {secs:.2f} s{score}  {text}")
    for name, values in rows.items():
        if values:
            print(f"\n{name}: {len(values)} recordings, mean WER {statistics.mean(values):.3f}, median {statistics.median(values):.3f}")


if __name__ == "__main__":
    asyncio.run(main())
