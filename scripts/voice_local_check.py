"""Does the voice on this machine speak? Speaks two sentences with it and says how long each took.

    cd /opt/sofa-health/src && sudo -u sofa /opt/sofa-health/venv/bin/python -m scripts.voice_local_check

The voice is the one PIPER_MODEL points to (deploy/setup_voice.sh sets it up). Nothing is sent anywhere.
"""

import asyncio
import io
import time
import wave

from sofa.clients.tts import build_local_voice
from sofa.config import get_settings

SENTENCES = (
    "Hello, this is a test of the voice on this machine.",
    "The nearest place is Yaba Lakeview Clinic, a private clinic on Herbert Macaulay Way, less than a kilometre away. It is open until 6 in the evening.",
)


async def main() -> int:
    settings = get_settings()
    print(f"PIPER_MODEL: {settings.piper_model or 'not set'}")
    voice = build_local_voice(settings)
    if voice is None:
        print("No voice on this machine: the model is not set, the file is missing or the piper package is not installed. See deploy/setup_voice.sh")
        return 1
    print(f"voice: {voice.tag}")
    for text in SENTENCES:
        started = time.perf_counter()
        audio = await voice.synthesize(text, "en")
        took = time.perf_counter() - started
        with wave.open(io.BytesIO(audio)) as w:
            seconds = w.getnframes() / w.getframerate()
        print(f"   {len(text.split()):>2} words: {took:.2f} s to make {seconds:.1f} s of speech")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
