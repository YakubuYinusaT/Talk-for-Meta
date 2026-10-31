"""Fixes after an outside review: an honest /health, no published default secret on a public address, a demo page that hides what callers said unless asked to show it,
recordings downloaded only from the telephony provider's hosts, and recordings deleted after their time."""

import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sofa.audio import check_recording_address, fetch_recording
from sofa.config import Settings
from sofa.hardening import KNOWN_DEFAULTS, replace_known_defaults
from sofa.main import create_app
from sofa.models import Call, CallTurn
from sofa.services import retention
from tests.test_call_flow import MUSA, SPEC_CONVERSATION


def settings(tmp_path, **more):
    return Settings(database_url="sqlite://", storage_dir=str(tmp_path), at_callback_secret="a-real-secret", public_base_url="http://test", scheduler_enabled=False,
                    _env_file=None, **more)


# ---- /health says which voice callers hear ---------------------------------------------------------------------------------------------


def test_health_names_the_hosted_voice_and_whether_it_has_a_back_up_not_the_gpu_hosts_address(tmp_path):
    bare = TestClient(create_app(settings(tmp_path, tts_provider="spitch", spitch_api_key="k"))).get("/health").json()
    assert bare["tts"] == "spitch, no back-up voice" and "mock" not in bare["tts"]
    backed = TestClient(create_app(settings(tmp_path, tts_provider="spitch", spitch_api_key="k", piper_model="/voices/x.onnx"))).get("/health").json()
    assert backed["tts"] == "spitch, with a voice on this machine as back-up"
    assert TestClient(create_app(settings(tmp_path))).get("/health").json()["tts"] == "inprocess://mock"      # no hosted voice: the GPU host's, as before
    assert "streamed_calls" in bare


# ---- no secret that is published with the code guards anything on a public address ----------------------------------------------------------


def test_on_an_https_address_every_secret_left_at_its_published_value_is_replaced():
    s = Settings(_env_file=None, public_base_url="https://talk.example")
    assert all(getattr(s, name) == default for name, default in KNOWN_DEFAULTS.items())
    replaced = replace_known_defaults(s)
    assert set(replaced) == {n.upper() for n in KNOWN_DEFAULTS}
    assert all(getattr(s, name) != default and not getattr(s, name).startswith("dev-") and len(getattr(s, name)) >= 24 for name, default in KNOWN_DEFAULTS.items())
    assert len({getattr(s, name) for name in KNOWN_DEFAULTS}) == len(KNOWN_DEFAULTS)      # each one different


def test_a_secret_somebody_set_is_left_alone_and_a_local_address_is_not_touched():
    s = Settings(_env_file=None, public_base_url="https://talk.example", admin_token="chosen-by-the-owner")
    replace_known_defaults(s)
    assert s.admin_token == "chosen-by-the-owner" and s.vendor_token != "dev-vendor-token"
    local = Settings(_env_file=None, public_base_url="http://localhost:8000")
    assert replace_known_defaults(local) == [] and local.admin_token == "dev-admin-token"


def test_the_application_applies_it_at_start(tmp_path):
    app = create_app(Settings(database_url="sqlite://", storage_dir=str(tmp_path), public_base_url="https://talk.example", scheduler_enabled=False, _env_file=None))
    assert app.state.svc.settings.admin_token != "dev-admin-token" and app.state.svc.settings.at_callback_secret != "dev-secret"
    assert TestClient(app).get("/voice/inbound/dev-secret").status_code == 404      # the published callback secret opens nothing


# ---- the demo page hides what callers said unless it is told to show it ------------------------------------------------------------------


def test_the_demo_page_shows_what_happened_but_not_what_was_said_by_default(client, app, call):
    app.state.svc.settings.demo_page_enabled = True
    call(SPEC_CONVERSATION, caller=MUSA)
    html = client.get("/demo").text
    assert "not what callers said" in html and "not shown" in html
    assert "Indomie" not in html and MUSA not in html and MUSA[-4:] in html
    app.state.svc.settings.demo_show_calls = True
    shown = client.get("/demo").text
    assert "Indomie" in shown and "not what callers said" not in shown


# ---- recordings come only from the telephony provider ----------------------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_only_https_and_the_listed_hosts_and_their_subdomains_are_allowed():
    allowed = ("africastalking.com",)
    check_recording_address("https://voice.africastalking.com/rec/1.mp3", allowed)
    check_recording_address("https://africastalking.com/rec/1.mp3", allowed)
    for bad in ("http://voice.africastalking.com/1.mp3", "https://evil.example/1.mp3", "https://africastalking.com.evil.example/1.mp3", "https://notafricastalking.com/1.mp3",
                "https://169.254.169.254/latest/meta-data", "https://localhost/x", "file:///etc/passwd", "ftp://voice.africastalking.com/x"):
        with pytest.raises(ValueError):
            check_recording_address(bad, allowed)
    check_recording_address("https://anything.example/x", ())      # an empty list means no restriction


@pytest.mark.anyio
async def test_a_blocked_address_is_refused_before_any_request_is_sent():
    seen = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, content=b"AUDIO")))
    with pytest.raises(ValueError):
        await fetch_recording("http://169.254.169.254/latest/meta-data", http=http, allowed=("africastalking.com",))
    assert seen == []
    assert await fetch_recording("https://voice.africastalking.com/1.mp3", wait=0, http=http, allowed=("africastalking.com",)) == b"AUDIO" and len(seen) == 1


@pytest.mark.anyio
async def test_a_redirect_off_the_listed_hosts_is_not_followed_and_one_within_them_is():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "voice.africastalking.com" and request.url.path == "/away":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        if request.url.host == "voice.africastalking.com" and request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "https://cdn.africastalking.com/final.mp3"})
        return httpx.Response(200, content=b"AUDIO")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        await fetch_recording("https://voice.africastalking.com/away", wait=0, http=http, allowed=("africastalking.com",))
    assert all("169.254" not in u for u in seen)
    assert await fetch_recording("https://voice.africastalking.com/moved", wait=0, http=http, allowed=("africastalking.com",)) == b"AUDIO"


def test_recordings_are_limited_to_the_telephony_providers_host_by_default(app):
    assert app.state.svc.settings.recording_hosts == ("africastalking.com", "at-internal.com")
    # the address a real recording came from on the live number (seen on 8 recordings): its sub-domains are allowed
    check_recording_address("https://gigantic.keller-shockley.at-internal.com/rec.mp3", app.state.svc.settings.recording_hosts)
    assert Settings(_env_file=None, recording_allowed_hosts=" A.example , .b.example ").recording_hosts == ("a.example", "b.example")


# ---- recordings are deleted after their time -------------------------------------------------------------------------------------------


def make_turn(svc, tmp_path, days_old, name):
    path = tmp_path / "audio_in" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"RIFF")
    with svc.session_factory() as db:
        call = Call(provider_session_id=name, from_number="+2348055550101", to_number="+2348000009999")
        db.add(call)
        db.flush()
        turn = CallTurn(call_id=call.id, seq=1, audio_path=str(path), created_at=datetime.now(timezone.utc) - timedelta(days=days_old))
        db.add(turn)
        db.commit()
    return path


def test_old_recordings_are_deleted_and_recent_ones_are_kept(app, tmp_path):
    svc = app.state.svc
    svc.settings.storage_dir, svc.settings.recording_retention_days = str(tmp_path), 30
    old, recent = make_turn(svc, tmp_path, 45, "old.wav"), make_turn(svc, tmp_path, 3, "recent.wav")
    assert retention.purge_recordings(svc) == 1
    assert not old.exists() and recent.exists()
    with svc.session_factory() as db:
        paths = {t.audio_path for t in db.scalars(select(CallTurn))}
    assert str(recent) in paths and str(old) not in paths and None in paths      # the record stays, without the sound
    assert retention.purge_recordings(svc) == 0                                  # nothing more to do


def test_a_recording_no_turn_points_to_is_swept_by_age(app, tmp_path):
    svc = app.state.svc
    svc.settings.storage_dir, svc.settings.recording_retention_days = str(tmp_path), 30
    (tmp_path / "audio_in").mkdir(exist_ok=True)
    orphan = tmp_path / "audio_in" / "orphan.wav"
    orphan.write_bytes(b"RIFF")
    long_ago = time.time() - 45 * 86400
    os.utime(orphan, (long_ago, long_ago))
    assert retention.purge_recordings(svc) == 1 and not orphan.exists()


def test_with_a_retention_of_zero_nothing_is_deleted(app, tmp_path):
    svc = app.state.svc
    svc.settings.storage_dir, svc.settings.recording_retention_days = str(tmp_path), 0
    old = make_turn(svc, tmp_path, 400, "kept.wav")
    assert retention.purge_recordings(svc) == 0 and old.exists()
