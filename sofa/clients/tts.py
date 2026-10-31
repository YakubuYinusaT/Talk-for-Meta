import asyncio
import logging

from .http import RetryingClient, http_for, tls_context


class TTSClient:
    """POST /tts (text, language) -> WAV bytes. YarnGPT2 on the GPU host."""

    def __init__(self, url: str, api_key: str = ""):
        self.http, self.base = http_for(url, api_key)

    async def synthesize(self, text: str, language: str) -> bytes:
        resp = await self.http.post(f"{self.base}/tts", json={"text": text, "language": language})
        resp.raise_for_status()
        return resp.content

log = logging.getLogger("sofa.tts")


def _wav(pcm: bytes, rate: int) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _repair_wav(raw: bytes) -> bytes:
    """Some services stream a WAV whose header says the length is unknown (0xFFFFFFFF), with extra chunks before the audio.
    Walk the chunks, take what is really there, and write a normal header so players and length checks agree."""
    import struct
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return raw
    pos, rate, channels = 12, 24000, 1
    while pos + 8 <= len(raw):
        name, size = raw[pos:pos + 4], struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = pos + 8
        if name == b"fmt " and body + 16 <= len(raw):
            channels, rate = struct.unpack("<HI", raw[body + 2:body + 8])
        elif name == b"data":
            pcm = raw[body:body + size] if body + size <= len(raw) else raw[body:]
            pcm = pcm[: len(pcm) - len(pcm) % (2 * channels)]
            return _wav(pcm, rate) if channels == 1 else raw
        pos = body + size + (size & 1)
    return raw


class ElevenLabsTTS:
    """ElevenLabs text to speech. Asks for 16 kHz raw audio and returns it as a WAV."""
    tag = "elevenlabs"

    def __init__(self, api_key: str, voice_id: str, model: str = "eleven_multilingual_v2", speed: float = 0.95):
        import httpx
        self.voice_id, self.model, self.speed = voice_id, model, speed
        self.tag = f"elevenlabs-{voice_id}-{model}-{speed}"
        self.http = RetryingClient(base_url="https://api.elevenlabs.io", timeout=30.0, headers={"xi-api-key": api_key}, verify=tls_context())

    async def synthesize(self, text: str, language: str) -> bytes:
        resp = await self.http.post(
            f"/v1/text-to-speech/{self.voice_id}", params={"output_format": "pcm_16000"},
            json={"text": text, "model_id": self.model, "voice_settings": {"speed": self.speed}})
        resp.raise_for_status()
        return _wav(resp.content, 16000)


class AzureTTS:
    """Azure AI Speech. SSML in, 16 kHz WAV out. The Nigerian English voices are en-NG-EzinneNeural and en-NG-AbeoNeural."""

    def __init__(self, key: str, region: str, voice: str = "en-NG-EzinneNeural", speed: float = 0.95):
        import httpx
        self.voice, self.speed = voice, speed
        self.tag = f"azure-{voice}-{speed}"
        self.http = RetryingClient(
            base_url=f"https://{region}.tts.speech.microsoft.com", timeout=30.0, verify=tls_context(),
            headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                     "X-Microsoft-OutputFormat": "riff-16khz-16bit-mono-pcm", "User-Agent": "sofa"})

    async def synthesize(self, text: str, language: str) -> bytes:
        from xml.sax.saxutils import escape
        rate = f"{round((self.speed - 1) * 100):+d}%"
        lang = self.voice[:5]
        ssml = (f"<speak version='1.0' xml:lang='{lang}'><voice name='{self.voice}'>"
                f"<prosody rate='{rate}'>{escape(text)}</prosody></voice></speak>")
        resp = await self.http.post("/cognitiveservices/v1", content=ssml.encode("utf-8"))
        resp.raise_for_status()
        return resp.content


class SpitchTTS:
    """Spitch (Nigerian company): English, Yoruba, Hausa and Igbo voices. POST /v1/speech returns a WAV."""

    def __init__(self, api_key: str, voices: dict[str, str], speed: float = 1.0):
        import httpx
        self.voices, self.speed = voices, min(1.2, max(0.7, speed))
        self._slots = asyncio.Semaphore(2)  # Tier 1 allows 3 calls at once; two leaves room for the one still being counted as a call finishes
        self.tag = "spitch-" + "-".join(f"{k}{v}" for k, v in sorted(voices.items())) + f"-{self.speed}"
        self.http = RetryingClient(base_url="https://api.spitch.app", timeout=30.0,
                                  headers={"Authorization": f"Bearer {api_key}"}, verify=tls_context())

    async def synthesize(self, text: str, language: str) -> bytes:
        body = {"text": text, "language": language, "voice": self.voices[language], "speed": self.speed, "format": "wav"}
        async with self._slots:
            for pause in (0.7, 1.5, 3.0, None):  # "too many at once" is asked again after a growing pause, up to three times, before it counts as a failure
                resp = await self.http.post("/v1/speech", json=body)
                if resp.status_code != 429 or pause is None:
                    break
                await asyncio.sleep(pause)
        resp.raise_for_status()
        return _repair_wav(resp.content)


class PiperTTS:
    """A voice that runs on this machine (Piper, open source): no account, no credit, no network. English only. It speaks when the hosted voice cannot, so a
    caller hears a real voice instead of silence; it is also quick on a plain CPU (a sentence in well under a second)."""

    languages = {"en"}

    def __init__(self, model_path: str, speed: float = 1.0):
        from piper import PiperVoice  # a package this machine may not have: build_tts only asks for it when a model is set

        self.voice = PiperVoice.load(model_path)
        self.length_scale = 1.0 / min(1.3, max(0.7, speed))
        self.tag = "piper-" + model_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].replace(".onnx", "")
        self._slots = asyncio.Semaphore(2)

    def _speak(self, text: str) -> bytes:
        import io
        import wave

        try:
            from piper import SynthesisConfig
            config = SynthesisConfig(length_scale=self.length_scale)
        except ImportError:  # an older release without the setting: the voice's own pace
            config = None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            if config is not None:
                self.voice.synthesize_wav(text, w, syn_config=config)
            else:
                self.voice.synthesize_wav(text, w)
        return buf.getvalue()

    async def synthesize(self, text: str, language: str) -> bytes:
        async with self._slots:
            return await asyncio.to_thread(self._speak, text)


MIN_WORDS = 4         # a piece shorter than this is joined to its neighbour
PAUSE_SECONDS = 0.3   # quiet between two spoken sentences
LEAD_SECONDS = 0.25   # quiet before the first word: players that wake their speaker on play swallow the start of a clip ("Done." went unheard)


def split_sentences(text: str) -> list[str]:
    """One piece per sentence, each made and spoken on its own, one after the other, because a long text read in one go loses its last sentence
    (a closing question like "Anything else?" went unheard). A piece under four words is joined to its neighbour: measured on the first real
    calls, the voice puts junk in front of very short clips ("Okay." came out wrong 7 times in 7, "Got it." 4 in 8) and not in longer ones (0 in 24)."""
    import re
    pieces = [p.strip() for p in re.split(r"(?<=[.?!])\s+", text.strip()) if p.strip()]
    while len(pieces) > 1:
        short = next((i for i, p in enumerate(pieces) if len(p.split()) < MIN_WORDS), None)
        if short is None:
            break
        j = short + 1 if short + 1 < len(pieces) else short - 1  # the sentence after it, or for the last one the sentence before
        lo, hi = sorted((short, j))
        pieces[lo:hi + 1] = [pieces[lo] + " " + pieces[hi]]
    return pieces


def join_wavs(wavs: list[bytes], pause: float = PAUSE_SECONDS, lead: float = LEAD_SECONDS) -> bytes:
    """Put WAV clips (same format) one after another with a quiet gap between them, and a little quiet before the first."""
    import io
    import wave
    frames, params = [], None
    for raw in wavs:
        with wave.open(io.BytesIO(raw)) as w:
            if params is None:
                params = w.getparams()
            frames.append(w.readframes(w.getnframes()))
    def quiet(seconds: float) -> bytes:
        return bytes(int(params.framerate * seconds) * params.sampwidth * params.nchannels)
    return _wav_params(quiet(lead) + quiet(pause).join(frames), params)


def _wav_params(pcm: bytes, params) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(pcm)
    return buf.getvalue()


class FallbackAudio(bytes):
    """Audio made by the stand-in voice because the hosted one failed. It is spoken this once and never kept as the phrase's permanent audio."""


class RoutedTTS:
    """Speaks the chosen languages with the hosted voice and every other language with the GPU host's voice. When the hosted voice fails, a voice on this
    machine (`local`, if there is one for the language) speaks instead; after a refusal for credit or a key, or a network fault, the hosted voice is left alone
    for a while, so each sentence does not wait for a failure first."""

    PAUSE_AFTER = {401: 600, 402: 600, 403: 600}  # seconds the hosted voice is not asked after this answer (no credit, a bad key): asking again only waits
    PAUSE_OTHER = 20

    def __init__(self, hosted, default, languages: set[str], local=None):
        self.hosted, self.default, self.languages, self.local = hosted, default, languages, local
        self.tag = hosted.tag
        self.skip_until = 0.0

    async def synthesize(self, text: str, language: str) -> bytes:
        import time

        if language in self.languages:
            if time.monotonic() >= self.skip_until:
                try:
                    pieces = split_sentences(text) or [text]
                    clips = await asyncio.gather(*(self.hosted.synthesize(piece, language) for piece in pieces))
                    return join_wavs(list(clips))
                except Exception as exc:  # out of credit, a network fault: a caller still hears a voice rather than a broken call
                    response = getattr(exc, "response", None)
                    status = response.status_code if response is not None else None
                    pause = self.PAUSE_AFTER.get(status, self.PAUSE_OTHER)
                    self.skip_until = time.monotonic() + pause
                    log.warning("the hosted voice failed (%s%s); using the %s for %d s", type(exc).__name__,
                                f", HTTP {status}: {response.text[:120]}" if response is not None else "", "voice on this machine" if self.local else "GPU host's voice", pause)
            if self.local is not None and language in self.local.languages:
                return FallbackAudio(await self.local.synthesize(text, language))
            return FallbackAudio(await self.default.synthesize(text, language))
        return await self.default.synthesize(text, language)


def build_tts(settings):
    default = TTSClient(settings.tts_url, settings.gpu_api_key)
    provider = (settings.tts_provider or "yarngpt").strip().lower()
    if provider == "yarngpt":
        return default
    langs = {l.strip() for l in settings.tts_provider_languages.split(",") if l.strip()}
    if provider == "elevenlabs":
        if not (settings.elevenlabs_api_key and settings.elevenlabs_voice_id):
            raise ValueError("TTS_PROVIDER=elevenlabs needs ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID")
        hosted = ElevenLabsTTS(settings.elevenlabs_api_key, settings.elevenlabs_voice_id,
                               settings.elevenlabs_model, settings.tts_hosted_speed)
    elif provider == "azure":
        if not (settings.azure_speech_key and settings.azure_speech_region):
            raise ValueError("TTS_PROVIDER=azure needs AZURE_SPEECH_KEY and AZURE_SPEECH_REGION")
        hosted = AzureTTS(settings.azure_speech_key, settings.azure_speech_region,
                          settings.azure_voice_en, settings.tts_hosted_speed)
    elif provider == "spitch":
        if not settings.spitch_api_key:
            raise ValueError("TTS_PROVIDER=spitch needs SPITCH_API_KEY")
        voices = {"en": settings.spitch_voice_en, "yo": settings.spitch_voice_yo,
                  "ha": settings.spitch_voice_ha, "ig": settings.spitch_voice_ig}
        hosted = SpitchTTS(settings.spitch_api_key, voices, settings.tts_hosted_speed)
        langs = langs or set(voices)  # no list given: Spitch speaks every language it has a voice for
        langs &= set(voices)
    else:
        raise ValueError(f"unknown TTS_PROVIDER {provider!r} (use yarngpt, elevenlabs, azure or spitch)")
    return RoutedTTS(hosted, default, langs or {"en"}, local=build_local_voice(settings))


def build_local_voice(settings):
    """The voice on this machine, if a model is set and the package is there. Never a reason to fail at start-up: without it the GPU host's voice is the fallback."""
    import os

    model = (getattr(settings, "piper_model", "") or "").strip()
    if not model:
        return None
    if not os.path.exists(model):
        log.warning("PIPER_MODEL is set but %s does not exist: no voice on this machine", model)
        return None
    try:
        voice = PiperTTS(model, getattr(settings, "piper_speed", 1.0))
    except Exception as exc:
        log.warning("the voice on this machine could not be loaded (%s: %s)", type(exc).__name__, str(exc)[:160])
        return None
    log.info("voice on this machine: %s", voice.tag)
    return voice
