"""Audio plumbing: recording download, 16 kHz mono resampling, TTS cache, call audio storage."""

import asyncio
import hashlib
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote

import httpx

from .clients.tts import FallbackAudio


def mock_recording_url(text: str, lang: str = "en") -> str:
    """Simulator helper: a 'recording' whose bytes are MOCK[lang]:text (see mocks/gpu_mock.py)."""
    from urllib.parse import quote

    return "mock:" + quote(f"MOCK[{lang}]:{text}", safe="")


def mock_mixed_url(readings: dict) -> str:
    """Simulator helper: a code-mixed 'recording' that each language model reads differently:
    {"en": ("Mo fe carton two ti Peak", -0.9), "yo": ("mo fe carton meji ti pik", -0.5)}"""
    import json
    from urllib.parse import quote

    return "mock:" + quote("MOCKX:" + json.dumps({k: list(v) for k, v in readings.items()}), safe="")


def check_recording_address(url: str, allowed: tuple[str, ...]) -> None:
    """A recording is downloaded only over https and only from the telephony provider's own hosts (and their sub-domains): an address that arrives in a callback
    is not trusted to be a recording. An empty list means no restriction."""
    if not allowed:
        return
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in allowed):
        raise ValueError(f"a recording is not downloaded from {host or 'that address'}")


async def fetch_recording(url: str, attempts: int = 5, wait: float = 0.4, http: httpx.AsyncClient | None = None, allowed: tuple[str, ...] = ()) -> bytes:
    """Africa's Talking recording URLs expire, so copy the audio to our own storage per turn.

    The address arrives the moment the recording ends, and on a real call the file was not yet on their server ("404 Not Found"). So a missing or
    busy file is asked for again a few times, a little later each time, before the turn is given up."""
    if url.startswith("mock:"):
        return unquote(url[len("mock:"):]).encode("utf-8")
    check_recording_address(url, allowed)
    own = http is None
    http = http or httpx.AsyncClient(timeout=15, follow_redirects=not allowed)   # with a list of hosts every hop is checked here, so a redirect cannot lead elsewhere
    # On the first real call the address Africa's Talking sent ended "...T.mp3" and answered 404, while the same file without the stray "T" answered
    # 200. So the address as sent is tried first, then that one.
    plain = re.sub(r"T(\.[A-Za-z0-9]{2,4})$", r"\1", url.split("?")[0])
    candidates = [url] + ([plain] if plain != url.split("?")[0] else [])
    try:
        for attempt in range(attempts):
            last = None
            for candidate in candidates:
                try:
                    resp = await http.get(candidate)
                    for _ in range(3 if allowed else 0):
                        if not (resp.is_redirect and resp.headers.get("location")):
                            break
                        nxt = str(resp.url.join(resp.headers["location"]))
                        check_recording_address(nxt, allowed)
                        resp = await http.get(nxt)
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError):
                    last = None
                    continue
                if resp.status_code not in (403, 404, 408, 425, 429) and resp.status_code < 500:
                    resp.raise_for_status()
                    return resp.content
                last = resp
            if attempt == attempts - 1:
                if last is not None:
                    last.raise_for_status()
                raise httpx.ConnectError("the recording could not be reached")
            await asyncio.sleep(wait * (attempt + 1))
        raise RuntimeError("unreachable")
    finally:
        if own:
            await http.aclose()


def wav_rms(wav: bytes) -> float:
    """How loud a 16-bit mono WAV is (root mean square, 0 to 32767). Plain Python, no numpy: a phone turn is a few hundred thousand samples."""
    import io
    import wave
    from array import array
    if wav.startswith((b"MOCK[", b"MOCKX:")):
        return 10_000.0  # the simulator's stand-in recordings are not audio
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2:
                return 10_000.0
            samples = array("h")
            samples.frombytes(w.readframes(w.getnframes()))
    except (wave.Error, EOFError):
        return 10_000.0
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


# Silence is cut from the start and the end only (the second pass runs on the reversed sound), never from the middle: a pause inside a sentence stays.
TRIM_SILENCE = ("silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB,areverse,"
                "silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB,areverse")
MIN_WAV_BYTES = 6000  # under about a fifth of a second of speech after trimming: use the recording as it came instead


def _ffmpeg_16k(raw: bytes, trim: bool) -> bytes:
    cmd = ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", *(["-af", TRIM_SILENCE] if trim else []), "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"]
    proc = subprocess.run(cmd, input=raw, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg resample failed: {proc.stderr.decode(errors='replace')[:200]}")
    return proc.stdout


def to_16k_mono(raw: bytes) -> bytes:
    """Resample phone audio (8 kHz) to the 16 kHz mono the speech models expect, with the silence at the start and the end cut away, so the recogniser is sent
    only the speech (the recording ends only after seconds of silence, and the time to read it grows with its length).
    Resampling cannot restore lost frequencies: measure accuracy on real calls (scripts/asr_gate.py)."""
    if raw.startswith((b"MOCK[", b"MOCKX:")):
        return raw
    try:
        trimmed = _ffmpeg_16k(raw, trim=True)
        if len(trimmed) >= MIN_WAV_BYTES:
            return trimmed
    except RuntimeError:
        pass  # an ffmpeg that does not know the filter, or a recording it cannot trim: the plain conversion below still works
    return _ffmpeg_16k(raw, trim=False)


SLIN_RATE = 8000  # the telephone line: 8 kHz, 16-bit signed little-endian, mono ("slin")


def pcm8_to_wav16(pcm: bytes) -> bytes:
    """Telephone audio as the 16 kHz mono WAV the speech model reads. Each gap between two samples gets their average, which is all a phone line
    can carry anyway (resampling cannot restore lost frequencies). Plain Python: a long turn is 100,000 samples, a few milliseconds of work."""
    import io
    import sys
    import wave
    from array import array
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    out = array("h")
    for a, b in zip(samples, samples[1:]):
        out.append(a)
        out.append((a + b) >> 1)
    if samples:
        out.extend((samples[-1], samples[-1]))
    if sys.byteorder == "big":
        out.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(2 * SLIN_RATE)
        w.writeframes(out.tobytes())
    return buf.getvalue()


def _wav_to_slin_plain(wav: bytes) -> bytes:
    """The no-ffmpeg way (a laptop, a test): each output sample is the average of the input samples it covers."""
    import io
    import sys
    import wave
    from array import array
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2:
                return b""
            rate, channels = w.getframerate(), w.getnchannels()
            samples = array("h")
            samples.frombytes(w.readframes(w.getnframes()))
    except (wave.Error, EOFError):
        return b""
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = array("h", (sum(samples[i:i + channels]) // channels for i in range(0, len(samples) - channels + 1, channels)))
    step = rate / SLIN_RATE
    out = array("h")
    for i in range(int(len(samples) / step)):
        lo = int(i * step)
        hi = max(lo + 1, int((i + 1) * step))
        window = samples[lo:hi]
        out.append(sum(window) // len(window))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def to_slin8k(wav: bytes) -> bytes:
    """A voice clip (any WAV) as raw 8 kHz telephone audio, ready to be sent down a streamed call. ffmpeg when it is there (it filters properly
    before it drops to 8 kHz); the plain averaging above when it is not."""
    try:
        proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ar", str(SLIN_RATE), "-ac", "1", "-f", "s16le", "pipe:1"],
                              input=wav, capture_output=True)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except FileNotFoundError:
        pass
    return _wav_to_slin_plain(wav)


FALLBACK_KEPT_SECONDS = 120


class AudioService:
    """Reply audio cached by hash of (language, text). Fixed phrases are pre-generated at deploy time."""

    def __init__(self, settings, tts):
        self.s, self.tts = settings, tts
        AudioService.voice_tag = getattr(tts, "tag", "")
        self.out_dir = Path(settings.storage_dir) / "audio_out"
        self.in_dir = Path(settings.storage_dir) / "audio_in"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.in_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    voice_tag = ""  # set when a hosted voice is chosen, so audio made by another voice is not reused

    @classmethod
    def key(cls, text: str, lang: str) -> str:
        salt = f"{cls.voice_tag}|" if cls.voice_tag else ""
        return hashlib.sha256(f"{salt}{lang}|{text}".encode()).hexdigest()[:24]

    def url_for_key(self, key: str) -> str:
        return f"{self.s.public_base_url.rstrip('/')}/audio/{key}.wav"

    async def speak(self, text: str, lang: str) -> tuple[str, str]:
        """Return (public url, storage path), synthesising only on a cache miss. Audio the stand-in voice made because the hosted voice failed is kept for
        a couple of minutes only, so one bad moment never leaves a phrase silent for good."""
        key = self.key(text, lang)
        path = self.out_dir / f"{key}.wav"
        marker = path.with_suffix(".fallback")
        if path.exists() and marker.exists():
            try:
                stale = time.time() - float(marker.read_text() or 0) > FALLBACK_KEPT_SECONDS
            except ValueError:
                stale = True
            if stale:
                path.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
        if not path.exists():
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                if not path.exists():
                    audio = await self.tts.synthesize(text, lang)
                    path.write_bytes(audio)
                    if isinstance(audio, FallbackAudio):
                        marker.write_text(str(time.time()))
                    else:
                        marker.unlink(missing_ok=True)
        return self.url_for_key(key), str(path)

    def is_ready(self, text: str, lang: str) -> bool:
        """Is the whole of this text already voiced and stored (a fixed phrase, or a reply heard before)? A stand-in made while the real voice was down does not count."""
        path = self.out_dir / f"{self.key(text, lang)}.wav"
        return path.exists() and not path.with_suffix(".fallback").exists()

    def pieces(self, text: str, lang: str, longest: int = 24) -> list[str]:
        """A reply as sentences, so the first can be spoken while the rest is still being voiced. Text that is already voiced whole stays whole (its audio is
        stored under the whole text). A very short sentence is joined to the next one, and a very long one is cut at a comma."""
        text = text.strip()
        if self.is_ready(text, lang) or len(text.split()) <= 12:
            return [text]
        sentences: list[str] = []
        for part in re.split(r"(?<=[.!?])\s+", text):
            if sentences and len(sentences[-1].split()) < 5:
                sentences[-1] += " " + part
            else:
                sentences.append(part)
        out: list[str] = []
        for sentence in sentences:
            if len(sentence.split()) <= longest:
                out.append(sentence)
                continue
            current = ""
            for chunk in re.split(r"(?<=,)\s+", sentence):
                if current and len((current + " " + chunk).split()) > longest:
                    out.append(current)
                    current = chunk
                else:
                    current = f"{current} {chunk}".strip()
            if current:
                out.append(current)
        return [p for p in out if p]

    def path_for_url(self, url: str) -> Path:
        """Where the clip behind one of this service's own audio addresses is stored."""
        return self.out_dir / url.rsplit("/", 1)[-1]

    async def telephone_audio(self, wav_path: Path) -> bytes:
        """A stored clip as 8 kHz telephone audio for a streamed call. The converted copy sits beside the clip and is made once; a clip that was
        made again (a stand-in voice replaced by the real one) is converted again."""
        pcm_path = wav_path.with_suffix(".s8k")
        if pcm_path.exists() and pcm_path.stat().st_mtime >= wav_path.stat().st_mtime:
            return pcm_path.read_bytes()
        pcm = await asyncio.to_thread(lambda: to_slin8k(wav_path.read_bytes()))
        if pcm:
            pcm_path.write_bytes(pcm)
        return pcm

    async def speak_telephone(self, text: str, lang: str) -> bytes:
        return await self.telephone_audio(Path((await self.speak(text, lang))[1]))

    def save_call_audio(self, call_id, seq: int, raw: bytes) -> str:
        path = self.in_dir / f"{call_id}_{seq:03d}.wav"
        path.write_bytes(raw)
        return str(path)
