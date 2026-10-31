"""Speech to text from Meta's Muse Voice Transcribe: the request is what Meta's API expects, a heard sentence is good enough to act on, and silence is not."""

import asyncio
import json

import httpx

from sofa.clients import asr as asr_module
from sofa.clients.asr import MetaASRClient
from sofa.config import Settings
from sofa.container import build_asr

REAL_CLIENT = httpx.AsyncClient


def client_with(monkeypatch, handler, **kw):
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    c = MetaASRClient("https://api.meta.ai", "meta-key", **kw)
    c.http = REAL_CLIENT(transport=httpx.MockTransport(respond))
    return c, seen


def test_the_request_is_what_metas_api_expects(monkeypatch):
    c, seen = client_with(monkeypatch, lambda r: httpx.Response(200, json={"sessionId": "s", "transcript": "  book  an appointment in Surulere ", "audioDurationMs": 1500, "turns": []}),
                          keywords=("Surulere", "antenatal", ""))
    out = asyncio.run(c.transcribe(b"RIFFwav", "en"))
    request = seen[0]
    assert str(request.url) == "https://api.meta.ai/v1/asr/transcribe" and request.headers["accept"] == "application/json"
    body = request.content.decode("latin-1")
    assert 'name="request"' in body and 'name="audio"' in body and "audio/wav" in body
    part = json.loads(body.split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0])
    assert part == {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV", "mode": "ENDPOINTING", "languageBias": ["English"], "keywords": ["Surulere", "antenatal"]}
    assert (out.text, out.language, out.model) == ("book an appointment in Surulere", "en", "muse-voice-transcribe-1.0") and out.confidence > -1.3


def test_silence_is_not_acted_on(monkeypatch):
    c, _ = client_with(monkeypatch, lambda r: httpx.Response(200, json={"sessionId": "s", "transcript": "", "audioDurationMs": 900, "turns": []}))
    out = asyncio.run(c.transcribe(b"RIFFwav", "en"))
    assert out.text == "" and out.confidence < -1.3                      # below the gate the voice route uses, so it is treated as not heard


def test_an_error_from_meta_is_raised_for_the_voice_route_to_handle(monkeypatch):
    c, _ = client_with(monkeypatch, lambda r: httpx.Response(401, json={"error": {"code": "invalid_api_key"}}))
    try:
        asyncio.run(c.transcribe(b"RIFFwav", "en"))
        raise AssertionError("should have raised")
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 401


def test_the_recogniser_is_chosen_by_a_setting_and_given_the_health_names():
    plain = build_asr(Settings(_env_file=None))
    assert type(plain).__name__ == "ASRClient"
    meta = build_asr(Settings(_env_file=None, asr_provider="meta", asr_url="https://api.meta.ai", llm_api_key="k", services_enabled="lookup,health", asr_keywords="Talk"))
    assert isinstance(meta, MetaASRClient) and meta.base == "https://api.meta.ai"
    assert "Talk" in meta.keywords and "Surulere" in meta.keywords and "amoxicillin" in meta.keywords and "Oncology Care Centre Ikeja" in meta.keywords and "Aguda" in meta.keywords
    assert len(meta.keywords) <= 150 and all(len(k) <= 30 for k in meta.keywords)       # the API refuses a longer word
