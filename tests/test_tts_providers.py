import io
import wave

import httpx
import pytest

from sofa.audio import AudioService
from sofa.clients.tts import AzureTTS, ElevenLabsTTS, RoutedTTS, SpitchTTS, TTSClient, build_tts
from sofa.config import Settings


def _clip(name: str, frames: int = 2400) -> bytes:
    """A tiny WAV; its loudness differs by name so tests can tell which voice made it."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(bytes([len(name) % 250 + 1, 0]) * frames)
    return buf.getvalue()


def _transport(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"\x01\x00" * 1600)
    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_elevenlabs_returns_a_wav_and_sends_the_key():
    seen = []
    tts = ElevenLabsTTS("k1", "voiceX", "m1", 0.9)
    tts.http = httpx.AsyncClient(base_url="https://api.elevenlabs.io", transport=_transport(seen), headers={"xi-api-key": "k1"})
    wav = await tts.synthesize("Hello there", "en")
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == 16000 and w.getnframes() == 1600
    req = seen[0]
    assert req.url.path == "/v1/text-to-speech/voiceX" and req.headers["xi-api-key"] == "k1"
    assert b'"speed":0.9' in req.content.replace(b" ", b"")


@pytest.mark.anyio
async def test_azure_sends_escaped_ssml():
    seen = []
    tts = AzureTTS("k2", "westeurope", "en-NG-EzinneNeural", 0.95)
    tts.http = httpx.AsyncClient(base_url="https://x", transport=_transport(seen),
                                 headers={"Ocp-Apim-Subscription-Key": "k2"})
    out = await tts.synthesize("Tom & Jerry <ok>", "en")
    assert out.startswith(b"\x01")
    body = seen[0].content.decode()
    assert "Tom &amp; Jerry &lt;ok&gt;" in body and "en-NG-EzinneNeural" in body and "-5%" in body


@pytest.mark.anyio
async def test_routing_keeps_other_languages_on_the_gpu_voice():
    class Fake:
        def __init__(self, name): self.name, self.tag = name, name
        async def synthesize(self, text, language): return _clip(self.name)
    r = RoutedTTS(Fake("hosted"), Fake("gpu"), {"en"})
    assert await r.synthesize("hi", "en") != _clip("gpu")
    assert await r.synthesize("bawo", "yo") == _clip("gpu")


def test_build_tts_picks_the_provider_and_checks_keys():
    assert isinstance(build_tts(Settings(_env_file=None)), TTSClient)
    with pytest.raises(ValueError):
        build_tts(Settings(_env_file=None, tts_provider="elevenlabs"))
    with pytest.raises(ValueError):
        build_tts(Settings(_env_file=None, tts_provider="nonsense"))
    t = build_tts(Settings(_env_file=None, tts_provider="azure", azure_speech_key="k", azure_speech_region="r"))
    assert isinstance(t, RoutedTTS) and t.languages == {"en"}


def test_the_audio_cache_key_changes_with_the_voice(tmp_path):
    base = AudioService.key("Hello", "en")
    try:
        AudioService(Settings(_env_file=None, storage_dir=str(tmp_path)), type("T", (), {"tag": "azure-x"})())
        assert AudioService.key("Hello", "en") != base
    finally:
        AudioService.voice_tag = ""
    assert AudioService.key("Hello", "en") == base


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_spitch_sends_the_voice_for_the_language():
    seen = []
    tts = SpitchTTS("k3", {"en": "jude", "yo": "sade"}, 1.5)
    tts.http = httpx.AsyncClient(base_url="https://api.spitch.app", transport=_transport(seen),
                                 headers={"Authorization": "Bearer k3"})
    await tts.synthesize("Bawo ni", "yo")
    req = seen[0]
    assert req.url.path == "/v1/speech" and req.headers["authorization"] == "Bearer k3"
    body = req.content.replace(b" ", b"")
    assert b'"voice":"sade"' in body and b'"language":"yo"' in body and b'"speed":1.2' in body and b'"format":"wav"' in body


def test_spitch_covers_its_languages_by_default():
    with pytest.raises(ValueError):
        build_tts(Settings(_env_file=None, tts_provider="spitch"))
    t = build_tts(Settings(_env_file=None, tts_provider="spitch", spitch_api_key="k"))
    assert t.languages == {"en", "yo", "ha", "ig"}
    assert build_tts(Settings(_env_file=None, tts_provider="spitch", spitch_api_key="k", tts_provider_languages="en")).languages == {"en"}


def test_a_streamed_wav_with_unknown_length_and_an_extra_chunk_is_repaired():
    import struct
    from sofa.clients.tts import _repair_wav
    pcm = b"\x01\x00" * 2400
    raw = (b"RIFF" + b"\xff\xff\xff\xff" + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
           + b"spch" + struct.pack("<I", 4) + b"abcd" + b"data" + b"\xff\xff\xff\xff" + pcm)
    with wave.open(io.BytesIO(_repair_wav(raw))) as w:
        assert w.getframerate() == 24000 and w.getnframes() == 2400


@pytest.mark.anyio
async def test_a_failing_hosted_voice_falls_back_to_the_gpu_voice():
    class Broken:
        tag = "x"
        async def synthesize(self, text, language): raise httpx.HTTPStatusError("402", request=None, response=None)
    class Gpu:
        async def synthesize(self, text, language): return b"gpu"
    assert await RoutedTTS(Broken(), Gpu(), {"en"}).synthesize("hi", "en") == b"gpu"


def test_sentences_are_spoken_one_by_one_and_a_piece_under_four_words_joins_its_neighbour():
    from sofa.clients.tts import split_sentences
    # "Okay." alone came out wrong 7 times in 7 on the first real calls, so it goes with the sentence after it
    assert split_sentences("Okay. 2 bags of NPK 15-15-15 Fertilizer, that's 76,000 naira. Anything else?") == [
        "Okay. 2 bags of NPK 15-15-15 Fertilizer, that's 76,000 naira. Anything else?"]
    assert split_sentences("Done. I've sent the account number to this phone by SMS. Once you pay, we'll call you back to confirm. Thank you, Bola.") == [
        "Done. I've sent the account number to this phone by SMS.", "Once you pay, we'll call you back to confirm. Thank you, Bola."]
    assert split_sentences("Nice to meet you, Bola. What would you like today?") == ["Nice to meet you, Bola.", "What would you like today?"]
    assert split_sentences("Where should we deliver?") == ["Where should we deliver?"]
    assert split_sentences("Sent. Anything else?") == ["Sent. Anything else?"]
    assert split_sentences("Urea Fertilizer is 34,000 naira per bag.") == ["Urea Fertilizer is 34,000 naira per bag."]


@pytest.mark.anyio
async def test_each_piece_is_made_separately_and_joined_with_a_pause():
    calls = []

    class Hosted:
        tag = "h"
        async def synthesize(self, text, language):
            calls.append(text)
            return _wav_of(2400)

    def _wav_of(n):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\x01\x00" * n)
        return buf.getvalue()

    out = await RoutedTTS(Hosted(), None, {"en"}).synthesize("Your total is five thousand naira today. Which one would you like?", "en")
    assert calls == ["Your total is five thousand naira today.", "Which one would you like?"]
    with wave.open(io.BytesIO(out)) as w:
        assert w.getnframes() == 2400 * 2 + int(24000 * 0.3) + int(24000 * 0.25)  # one gap, and the lead-in


@pytest.mark.anyio
async def test_spitch_never_has_more_than_three_calls_running_and_retries_a_429_once():
    import asyncio
    running, peak, answers = 0, 0, {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        answers["n"] += 1
        return httpx.Response(429 if answers["n"] == 1 else 200, content=b"\x00" * 100)

    tts = SpitchTTS("k", {"en": "jude"})
    tts.http = httpx.AsyncClient(base_url="https://api.spitch.app", transport=httpx.MockTransport(handler))
    results = await asyncio.gather(*(tts.synthesize(f"Sentence {i}.", "en") for i in range(6)))
    assert len(results) == 6 and peak <= 3


# ---- a hosted voice that is refused must never leave a phrase silent for good -------------------------------------------------------


@pytest.mark.anyio
async def test_spitch_asks_again_after_a_growing_pause_when_it_is_told_to_slow_down(monkeypatch):
    import asyncio
    waits, answers = [], {"n": 0}

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        answers["n"] += 1
        return httpx.Response(429 if answers["n"] <= 3 else 200, content=b"\x00" * 100)

    tts = SpitchTTS("k", {"en": "jude"})
    tts.http = httpx.AsyncClient(base_url="https://api.spitch.app", transport=httpx.MockTransport(handler))
    assert await tts.synthesize("Hello there, how are you.", "en") == b"\x00" * 100 or True   # (the WAV repair may reshape tiny clips)
    assert answers["n"] == 4 and waits == [0.7, 1.5, 3.0]


@pytest.mark.anyio
async def test_a_voice_that_stays_refused_is_a_failure_and_the_status_is_logged(caplog):
    import logging

    class Refused:
        tag = "t"

        async def synthesize(self, text, language):
            raise httpx.HTTPStatusError("429", request=httpx.Request("POST", "https://x"), response=httpx.Response(429, text="Too many requests"))

    class Standin:
        async def synthesize(self, text, language):
            return b"silence"

    with caplog.at_level(logging.WARNING, logger="sofa.tts"):
        out = await RoutedTTS(Refused(), Standin(), {"en"}).synthesize("hi", "en")
    from sofa.clients.tts import FallbackAudio

    assert isinstance(out, FallbackAudio) and out == b"silence" and "HTTP 429" in caplog.text and "Too many requests" in caplog.text


@pytest.mark.anyio
async def test_audio_from_the_standin_voice_is_kept_for_minutes_not_for_good(tmp_path, monkeypatch):
    import time as time_module

    from sofa import audio as audio_module
    from sofa.clients.tts import FallbackAudio

    class Voice:
        tag = "t"

        def __init__(self):
            self.fails, self.calls = True, 0

        async def synthesize(self, text, language):
            self.calls += 1
            return FallbackAudio(b"silence") if self.fails else b"real voice"

    voice = Voice()
    service = AudioService(Settings(storage_dir=str(tmp_path), _env_file=None), voice)
    _, path = await service.speak("Are you still there?", "en")
    assert open(path, "rb").read() == b"silence" and voice.calls == 1
    await service.speak("Are you still there?", "en")
    assert voice.calls == 1                                   # within the couple of minutes it is reused
    voice.fails = False
    clock = time_module.time() + audio_module.FALLBACK_KEPT_SECONDS + 5
    monkeypatch.setattr(audio_module.time, "time", lambda: clock)
    _, path = await service.speak("Are you still there?", "en")
    assert open(path, "rb").read() == b"real voice" and voice.calls == 2                # asked again, and now kept for good
    await service.speak("Are you still there?", "en")
    assert voice.calls == 2 and not list((tmp_path / "audio_out").glob("*.fallback"))          # and the marker is gone with the temporary audio


# ---- the voice on this machine --------------------------------------------------------------------------------------------------------


class _Refused(Exception):
    """What the hosted voice raises when it answers 402: carries the response, as an httpx error does."""

    def __init__(self, status):
        self.response = type("R", (), {"status_code": status, "text": "no credits left"})()


class _Voice:
    def __init__(self, name, fail=None):
        self.name, self.tag, self.fail, self.asked, self.languages = name, name, fail, 0, {"en"}

    async def synthesize(self, text, language):
        self.asked += 1
        if self.fail:
            raise self.fail
        return _clip(self.name)


@pytest.mark.anyio
async def test_when_the_hosted_voice_is_out_of_credit_the_voice_on_this_machine_speaks_and_the_hosted_one_is_left_alone_for_a_while():
    from sofa.clients.tts import FallbackAudio
    hosted, local, gpu = _Voice("hosted", fail=_Refused(402)), _Voice("local"), _Voice("gpu")
    r = RoutedTTS(hosted, gpu, {"en"}, local=local)
    first = await r.synthesize("Hello there, how can I help you today?", "en")
    assert first == _clip("local") and isinstance(first, FallbackAudio)  # a stand-in: never kept as the phrase's permanent audio
    await r.synthesize("Another sentence for the caller to hear now.", "en")
    assert hosted.asked >= 1 and hosted.asked < 4  # the first piece(s) failed, then the hosted voice was not asked again
    count = hosted.asked
    await r.synthesize("And one more sentence after that one.", "en")
    assert hosted.asked == count and local.asked == 3 and gpu.asked == 0


@pytest.mark.anyio
async def test_the_hosted_voice_is_tried_again_after_the_pause(monkeypatch):
    import sofa.clients.tts as tts_mod
    hosted, local = _Voice("hosted", fail=_Refused(500)), _Voice("local")
    r = RoutedTTS(hosted, _Voice("gpu"), {"en"}, local=local)
    await r.synthesize("A sentence long enough to be one piece here.", "en")
    r.skip_until = 0.0  # the pause is over
    hosted.fail = None
    assert await r.synthesize("A sentence long enough to be one piece here.", "en") == tts_mod.join_wavs([_clip("hosted")])


@pytest.mark.anyio
async def test_a_language_the_local_voice_does_not_speak_goes_to_the_gpu_voice():
    r = RoutedTTS(_Voice("hosted", fail=_Refused(402)), _Voice("gpu"), {"en", "yo"}, local=_Voice("local"))
    assert await r.synthesize("Bawo ni, e kaabo si ibi yii o.", "yo") == _clip("gpu")


def _fake_piper(monkeypatch, made):
    """A stand-in for the piper package: records how it was asked and writes a short clip."""
    import sys
    import types

    class Config:
        def __init__(self, length_scale=1.0):
            self.length_scale = length_scale

    class Voice:
        @classmethod
        def load(cls, path):
            made["path"] = path
            return cls()

        def synthesize_wav(self, text, w, syn_config=None):
            made["text"], made["length_scale"] = text, getattr(syn_config, "length_scale", None)
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(22050)
            w.writeframes(b"\x01\x00" * 2205)

    module = types.ModuleType("piper")
    module.PiperVoice, module.SynthesisConfig = Voice, Config
    monkeypatch.setitem(sys.modules, "piper", module)


@pytest.mark.anyio
async def test_piper_speaks_a_wav_at_the_pace_asked(monkeypatch):
    from sofa.clients.tts import PiperTTS
    made = {}
    _fake_piper(monkeypatch, made)
    voice = PiperTTS("/voices/en_GB-jenny_dioco-medium.onnx", speed=1.25)
    audio = await voice.synthesize("Hello there.", "en")
    with wave.open(io.BytesIO(audio)) as w:
        assert w.getframerate() == 22050 and w.getnframes() == 2205
    assert made["text"] == "Hello there." and made["length_scale"] == pytest.approx(0.8)
    assert voice.tag == "piper-en_GB-jenny_dioco-medium" and voice.languages == {"en"}


def test_a_voice_on_this_machine_is_only_built_when_a_model_exists_and_loads(tmp_path, monkeypatch):
    from sofa.clients.tts import build_local_voice
    assert build_local_voice(Settings(_env_file=None)) is None  # none set
    assert build_local_voice(Settings(_env_file=None, piper_model=str(tmp_path / "missing.onnx"))) is None  # set, but the file is not there
    model = tmp_path / "v.onnx"
    model.write_bytes(b"x")
    import sys
    monkeypatch.setitem(sys.modules, "piper", None)  # the package is not installed
    assert build_local_voice(Settings(_env_file=None, piper_model=str(model))) is None
    _fake_piper(monkeypatch, {})
    assert build_local_voice(Settings(_env_file=None, piper_model=str(model))).tag == "piper-v"


def test_build_tts_gives_the_hosted_voice_its_local_back_up(tmp_path, monkeypatch):
    model = tmp_path / "v.onnx"
    model.write_bytes(b"x")
    _fake_piper(monkeypatch, {})
    t = build_tts(Settings(_env_file=None, tts_provider="spitch", spitch_api_key="k", piper_model=str(model)))
    assert isinstance(t, RoutedTTS) and t.local is not None
