import json

from pydantic import BaseModel

from .http import http_for


class ASRResult(BaseModel):
    text: str
    confidence: float  # average log-probability; higher (closer to 0) is better
    model: str = ""
    language: str = ""


class ASRClient:
    """POST /asr (audio bytes, language) -> text, confidence. One N-ATLAS Whisper model per language."""

    def __init__(self, url: str, api_key: str = ""):
        self.http, self.base = http_for(url, api_key)

    async def transcribe(self, audio: bytes, language: str) -> ASRResult:
        resp = await self.http.post(
            f"{self.base}/asr",
            files={"audio": ("turn.wav", audio, "audio/wav")},
            data={"language": language},
        )
        resp.raise_for_status()
        return ASRResult(**resp.json())


MAX_KEYWORD = 30


class MetaASRClient:
    """Speech to text from Meta's Muse Voice Transcribe: one recording (16 kHz mono WAV, as the voice route prepares it) in, the words out. English only: the model
    covers 25 languages and Yoruba, Hausa, Igbo and Pidgin are not among them. `keywords` are words to listen for (place, clinic and medicine names); they bias
    recognition and do not force a spelling. The model reports no confidence, so a heard sentence gets a steady good score and an empty answer a bad one."""

    def __init__(self, url: str, api_key: str, model: str = "muse-voice-transcribe-1.0", keywords: tuple[str, ...] = ()):
        self.http, self.base = http_for(url, api_key)
        self.model = model
        self.keywords = [k for k in dict.fromkeys(keywords) if k and len(k) <= MAX_KEYWORD][:150]   # the API refuses a word longer than 30 characters

    async def transcribe(self, audio: bytes, language: str) -> ASRResult:
        request = {"model": self.model, "audioEncoding": "WAV", "mode": "ENDPOINTING", "languageBias": ["English"], **({"keywords": self.keywords} if self.keywords else {})}
        resp = await self.http.post(f"{self.base}/v1/asr/transcribe", headers={"Accept": "application/json"},
                                    files={"request": (None, json.dumps(request), "application/json"), "audio": ("turn.wav", audio, "audio/wav")})
        resp.raise_for_status()
        text = " ".join(str(resp.json().get("transcript", "")).split())
        return ASRResult(text=text, confidence=-0.3 if text else -3.0, model=self.model, language=language)
