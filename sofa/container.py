from dataclasses import dataclass, field

from .audio import AudioService
from .clients.asr import ASRClient, MetaASRClient
from .clients.llm import LLMClient
from .clients.paystack import PaystackClient
from .clients.search import build_search
from .clients.sms import SMSClient
from .clients.tts import build_tts
from .clients.voice import VoiceClient
from .config import Settings
from .gateway.bankauth import MockBankAuth
from .gateway.hub import Gateway, build_gateway
from .gateway.bankapi import build_bank
from .gateway.policy import VerificationPolicy
from .health.links import ProviderLinks
from .session import make_store


@dataclass
class Services:
    settings: Settings
    session_factory: object
    sessions: object
    asr: object          # ASRClient (our GPU host) or MetaASRClient (Muse Voice Transcribe)
    llm: LLMClient
    tts: object
    sms: SMSClient
    paystack: PaystackClient
    voice: VoiceClient
    audio: AudioService
    turn_tasks: dict = field(default_factory=dict)  # turn_id -> asyncio.Task (single-process; use Redis for multi-worker)
    gateway: Gateway = field(default_factory=build_gateway)  # registered service adapters, tool registry, verification policy
    bankauth: object = None  # checks PINs and one-time codes (the bank in production, a mock until then)
    bank: object = None      # the bank's own operations: balance, transfer, ... (a mock until the bank's sandbox)
    search: object = None    # where information questions are looked up (Google when its keys are set)
    connectors: object = None  # reaches the provider systems behind the health front desk (sofa/health/links.py)


def build_asr(settings: Settings):
    """Our own speech models on the GPU host, or Meta's Muse Voice Transcribe. With the health network on, the recogniser is also given its place, clinic and medicine names."""
    if settings.asr_provider != "meta":
        return ASRClient(settings.asr_url, settings.gpu_api_key)
    words = [w.strip() for w in settings.asr_keywords.split(",") if w.strip()]
    if settings.synthetic_hospital or "health" in {x.strip() for x in settings.services_enabled.split(",")}:
        from .health import data

        plain = {"clinic", "hospital", "centre", "health", "general", "medical", "community", "pharmacy", "everwell"}
        shortened = [" ".join(w for w in f.name.split() if w.lower() not in plain) for f in data.FACILITIES]     # "Aguda", "Yaba Lakeview": what a caller would say
        words += [a.name for a in data.AREAS.values()] + [d.aliases[0] if d.aliases else d.name for d in data.DRUGS] + [f.name for f in data.FACILITIES if f.kind != "pharmacy"] + shortened
    return MetaASRClient(settings.asr_url, settings.asr_api_key or settings.llm_api_key, settings.asr_model, tuple(dict.fromkeys(words)))


def build_services(settings: Settings, session_factory) -> Services:
    tts = build_tts(settings)
    sms = SMSClient(settings)
    gateway = build_gateway(settings)
    gateway.policy = VerificationPolicy(settings)
    return Services(
        settings=settings,
        session_factory=session_factory,
        sessions=make_store(settings.redis_url),
        asr=build_asr(settings),
        llm=LLMClient(settings.llm_url, settings.llm_model, settings.llm_api_key or settings.gpu_api_key, settings.llm_structured_mode, settings.llm_provider, settings.llm_reasoning_effort),
        tts=tts,
        sms=sms,
        paystack=PaystackClient(settings),
        voice=VoiceClient(settings),
        audio=AudioService(settings, tts),
        gateway=gateway,
        bankauth=MockBankAuth(settings, sms),
        bank=build_bank(settings),
        search=build_search(settings),
        connectors=ProviderLinks(settings),
    )
