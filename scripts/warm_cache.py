"""Pre-generate cached audio for every fixed phrase at deploy time (latency budget).

    python -m scripts.warm_cache

Only languages with real wording are warmed (English always; Yoruba, Hausa and Igbo only for the
keys that have translations). Nothing English is ever spoken in a Yoruba/Hausa/Igbo voice.
"""

import asyncio
from pathlib import Path

from sofa.config import get_settings
from sofa.container import build_services
from sofa.db import make_engine, make_session_factory
from sofa.dialogue import language, templates


PACE_SECONDS = 0.5          # a little time between phrases, so the voice service's rate limit is not reached by the warm-up itself
RETRIES = 6                 # a phrase the voice service refused is asked for again after 5, 10, 15 ... seconds


async def speak_properly(audio, text: str, lang: str, pace: float = PACE_SECONDS, pause: float = 5.0) -> bool:
    """Speak one phrase and keep its audio, but only if the real voice made it: audio the stand-in voice made because the real one was refused is thrown away
    and asked for again after a pause. False when the phrase could not be made in the end."""
    for attempt in range(RETRIES):
        _, path = await audio.speak(text, lang)
        marker = Path(path).with_suffix(".fallback")
        if not marker.exists():
            await asyncio.sleep(pace)
            return True
        Path(path).unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        await asyncio.sleep(pause * (attempt + 1))
    return False


async def main() -> None:
    settings = get_settings()
    svc = build_services(settings, make_session_factory(make_engine(settings.database_url)))
    count = failed = 0
    for attempt in range(1, language.MAX_ASKS + 1):  # the spoken "which language?" question
        for text, vlang in language.ask_prompts(settings.languages, attempt):
            ok = await speak_properly(svc.audio, text, vlang)
            count, failed = count + ok, failed + (not ok)
    for lang in settings.languages:
        for key, variants in templates.EN.items():
            for i, english in enumerate(variants):
                if "{" in english or not templates.has(key, lang):
                    continue
                text = templates.pick(key, lang, i)
                if "{" in text:
                    continue
                ok = await speak_properly(svc.audio, text, lang)
                count, failed = count + ok, failed + (not ok)
    print(f"cached {count} fixed phrases" + (f"; {failed} could not be made with the real voice and will be made when first needed" if failed else ""))


if __name__ == "__main__":
    asyncio.run(main())
