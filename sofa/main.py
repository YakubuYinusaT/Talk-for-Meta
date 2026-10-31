import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import HTTPException

from .config import Settings, get_settings
from .hardening import replace_known_defaults
from .container import build_services
from .db import init_db, make_engine, make_session_factory
from .routes import admin, demo, hub, overview, pages, provider, vendor, voice, webhooks
from .web.auth import LoginRequired

ATTRIBUTION = (
    "SOFA is a product of Connected Intelligence."
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO)
    replace_known_defaults(settings)  # on an https address no secret may still be the one published with the code
    engine = make_engine(settings.database_url)
    init_db(engine)
    svc = build_services(settings, make_session_factory(engine))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.synthetic_hospital:  # the provider systems behind the health front desk, named by the settings
            from .health.links import specs_from_settings

            health = svc.gateway.adapter("health")
            if health:
                health.set_providers(specs_from_settings(settings))

        from .services import scheduler

        task = asyncio.create_task(scheduler.loop(svc)) if settings.scheduler_enabled else None
        sip = None
        if settings.sip_enabled:  # streamed calls from the SIP trunk, through Asterisk on this machine (sofa/sip)
            from .sip.agent import SipServer

            sip = SipServer(svc)
            await sip.start()
        yield
        if sip:
            sip.stop()
        if task:
            task.cancel()
        engine.dispose()

    from .web.ui import profile

    # The generated API reference lists every route, the other side's pages included, so it exists only in the team's own build.
    open_docs = profile(settings) == "all"
    app = FastAPI(title="SOFA", lifespan=lifespan, docs_url="/docs" if open_docs else None, redoc_url=None, openapi_url="/openapi.json" if open_docs else None)
    app.state.svc = svc
    app.state.engine = engine
    app.include_router(voice.router)
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    app.include_router(pages.router)
    app.include_router(provider.router)
    app.include_router(vendor.router)
    app.include_router(demo.router)
    app.include_router(overview.router)
    app.include_router(hub.router)
    if settings.synthetic_hospital:  # made-up provider systems beside Talk, each reached only through its own signed connector (see sofa/health)
        from .health import register as health_register

        for slug, provider_app in health_register.create_apps(settings).items():
            app.mount(f"/synthetic/{slug}", provider_app)
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/static/ci-mark-red.png", status_code=307)

    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "web" / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def login_required(request, exc: LoginRequired):
        from urllib.parse import quote

        return RedirectResponse(f"{exc.login_url}?next={quote(exc.next_url)}", status_code=303)

    @app.get("/health")
    def health():
        hosted = settings.tts_provider != "yarngpt"   # the voice callers hear: the hosted one (with or without a back-up on this machine), or the GPU host's
        voice = f"{settings.tts_provider}, " + ("with a voice on this machine as back-up" if settings.piper_model else "no back-up voice") if hosted else settings.tts_url
        return {"ok": True, "sms": "mock" if settings.sms_is_mock else "live",
                "paystack": "mock" if settings.paystack_is_mock else "live",
                "asr": settings.asr_url, "llm": settings.llm_url, "tts": voice, "streamed_calls": settings.sip_enabled}

    @app.get("/audio/{name}")
    def audio(name: str):
        path = Path(settings.storage_dir) / "audio_out" / Path(name).name
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="audio/wav")

    return app

