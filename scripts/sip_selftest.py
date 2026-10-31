"""A test call through Asterisk into the streamed-call agent, without the trunk: a spoken sentence is played down the line as if a caller said it.
Run by `bash deploy/setup_sip.sh selftest`, which calls this twice around the call itself:

    python -m scripts.sip_selftest make ["sentence"]    the sentence in Talk's own voice, as an 8 kHz sound file Asterisk can play
    SELFTEST_SINCE=<epoch seconds> python -m scripts.sip_selftest result    what Talk heard, what it said and how long it took, from its own records

The caller number is a made-up one (+2348055550000), the one the [selftest] context gives the call.
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from sofa.config import get_settings
from sofa.container import build_services
from sofa.db import init_db, make_engine, make_session_factory
from sofa.models import Call, CallTurn

SAID = "I need help finding a clinic near Yaba"
SOUND = Path(os.environ.get("SELFTEST_SOUNDS", "/usr/share/asterisk/sounds/talk")) / "selftest.sln"  # raw 8 kHz 16-bit: no header for Asterisk to reject
CALLER = "+2348055550000"
LEAD_MS = 11000


def services():
    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    return build_services(settings, make_session_factory(engine))


def naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


async def make(sentence: str) -> int:
    svc = services()
    print(f"1. making the caller's voice: \"{sentence}\"")
    _, path = await svc.audio.speak(sentence, "en")
    # 11 seconds of quiet first (Talk greets meanwhile), then the sentence, then 30 seconds of quiet while it answers: the whole call is one continuous sound
    done = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-af", f"adelay={LEAD_MS}:all=1,apad=pad_dur=30", "-ar", "8000", "-ac", "1",
                           "-f", "s16le", str(SOUND)], capture_output=True)
    if done.returncode != 0:
        print("   ffmpeg failed:", done.stderr.decode(errors="replace")[:300])
        return 1
    SOUND.chmod(0o664)
    return 0


async def result() -> int:
    svc = services()
    since = datetime.fromtimestamp(float(os.environ.get("SELFTEST_SINCE", "0")), timezone.utc).replace(tzinfo=None)
    print("3. waiting for Talk to greet, hear the sentence and answer (up to 60 seconds)")
    turns = []
    for _ in range(60):
        with svc.session_factory() as db:
            call = db.scalar(select(Call).where(Call.from_number == CALLER).order_by(Call.created_at.desc()).limit(1))
            if call and naive(call.created_at) >= since:
                turns = list(db.scalars(select(CallTurn).where(CallTurn.call_id == call.id).order_by(CallTurn.seq)))
                if any(t.transcript and t.reply_text for t in turns):  # a turn where something was heard and answered
                    break
        await asyncio.sleep(1)
    if not turns:
        print("\nNo call reached Talk (no call record).")
        return 1
    for t in turns:
        lat = t.latency_ms or {}
        print(f"   heard : {t.transcript!r}   ({t.asr_model}, confidence {t.asr_confidence})")
        print(f"   said  : {(t.reply_text or '')[:300]!r}")
        print(f"   time  : speech read {lat.get('asr', '?')} ms, understood and acted {lat.get('understand_and_act', '?')} ms, "
              f"voice {lat.get('tts', '?')} ms, turn total {lat.get('total', '?')} ms")
    if not any(t.transcript and t.reply_text for t in turns):
        print("\nThe call reached Talk, but nothing was heard in it (only silence turns).")
        return 1
    print("\nthe call reached Talk, was heard and was answered.")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "result"
    raise SystemExit(asyncio.run(make(sys.argv[2] if len(sys.argv) > 2 else SAID) if mode == "make" else result()))
