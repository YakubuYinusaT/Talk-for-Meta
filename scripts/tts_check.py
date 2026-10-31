"""Does the hosted voice work from this machine? Speaks one short sentence and says what the voice service answered.

    cd /opt/sofa-health/src && sudo -u sofa /opt/sofa-health/venv/bin/python -m scripts.tts_check

Prints the answer's status and, when it is a refusal, the service's own message. The key is never printed.
"""

import asyncio

import httpx

from sofa.config import get_settings


async def main() -> int:
    s = get_settings()
    print(f"voice provider: {s.tts_provider}, English voice: {s.spitch_voice_en}, speed {s.tts_hosted_speed}, key {'set' if s.spitch_api_key else 'NOT SET'} ({len(s.spitch_api_key)} characters)")
    body = {"text": "Hello, this is a test of the voice.", "language": "en", "voice": s.spitch_voice_en, "speed": min(1.2, max(0.7, s.tts_hosted_speed)), "format": "wav"}
    async with httpx.AsyncClient(base_url="https://api.spitch.app", timeout=60, headers={"Authorization": f"Bearer {s.spitch_api_key}"}) as http:
        for attempt in (1, 2, 3):
            resp = await http.post("/v1/speech", json=body)
            kind = resp.headers.get("content-type", "")
            print(f"try {attempt}: HTTP {resp.status_code}, {kind}, {len(resp.content)} bytes" + ("" if resp.status_code == 200 else f" | {resp.text[:300]}"))
            if resp.status_code == 200:
                return 0
            await asyncio.sleep(2)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
