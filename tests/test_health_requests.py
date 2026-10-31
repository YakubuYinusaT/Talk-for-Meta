"""Requests for the care team: what Talk cannot settle for a caller (a question the patient guide does not cover, an emergency) is passed to a provider's staff, who see it
on their own front desk, answer it there, and have the answer texted back. The provider never receives a phone number."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sofa.health import data, followups
from sofa.health.provider import Provider, create_provider_app
from sofa.health.store import HospitalStore
from sofa.models import HealthRequest, Notification
from tests.test_health_calls import CALLER, FIXED, call, client, happ, hdb  # noqa: F401  (the fixtures)

STAFF = "+2348055550199"


def all_requests(happ):
    return [dict(r) for slug in happ.state.providers for r in happ.state.providers[slug].state.store.requests()]


@pytest.fixture
def store():
    return HospitalStore(facilities=data.CANCER.facilities)


# ---- the provider's own queue -----------------------------------------------------------------------------------------------------


def test_urgent_requests_come_first_then_the_oldest(store):
    t = [FIXED]
    store.clock = lambda: t[0]
    first = store.add_request("question", False, "what do I bring", "caller-a")
    t[0] = FIXED.replace(minute=40)
    second = store.add_request("question", False, "where do I park", "caller-b")
    third = store.add_request("emergency", False, "he is not breathing", "caller-c")
    assert [r["code"] for r in store.requests()] == [third["code"], first["code"], second["code"]]
    assert third["urgent"] is True and first["urgent"] is False      # an emergency is urgent whatever was asked


def test_an_answered_request_drops_below_the_open_ones_and_can_be_answered_only_once(store):
    a = store.add_request("question", False, "q1", "caller-a")
    b = store.add_request("question", False, "q2", "caller-b")
    assert store.reply_request(a["code"], "  Bring your   referral letter. ") is True
    assert store.reply_request(a["code"], "a second answer") is False       # once only
    assert store.reply_request(b["code"], "   ") is False                   # an empty answer is no answer
    rows = store.requests()
    assert [r["code"] for r in rows] == [b["code"], a["code"]] and rows[1]["reply"] == "Bring your referral letter."


def test_a_caller_sees_only_their_own_request(store):
    made = store.add_request("question", False, "q", "caller-a")
    assert store.request(made["code"], "caller-a")["status"] == "open"
    assert store.request(made["code"], "caller-b") is None


def test_the_provider_refuses_a_request_without_a_caller_a_summary_or_a_known_kind(store):
    provider = Provider(data.CANCER, store)
    assert provider.call("request", {"kind": "question", "summary": "q"}, {})["status"] == "error"
    assert provider.call("request", {"kind": "question", "summary": "  "}, {"caller_id": "c"})["status"] == "error"
    assert provider.call("request", {"kind": "gossip", "summary": "q"}, {"caller_id": "c"})["status"] == "error"
    made = provider.call("request", {"kind": "question", "summary": "q"}, {"caller_id": "c"})
    assert made["status"] == "ok" and made["data"]["request"]["code"].startswith("REQ-")
    assert provider.call("request_status", {"code": made["data"]["request"]["code"]}, {"caller_id": "c"})["data"]["request"]["status"] == "open"
    assert provider.call("request_status", {"code": made["data"]["request"]["code"]}, {"caller_id": "other"})["status"] == "error"


def test_the_desk_page_lists_requests_and_lets_staff_answer_only_with_the_desk_key(store):
    web = TestClient(create_provider_app(data.CANCER, "s", store, desk_key="desk-secret"))
    made = store.add_request("emergency", True, "he is not breathing", "caller-aaaa")
    public, staff = web.get("/").text, web.get("/?key=desk-secret").text
    assert "Requests for the care team (1 waiting)" in public and "he is not breathing" in public and "URGENT" in public and "Reply" not in public
    assert "<button>Reply</button>" in staff
    assert web.post("/desk/reply", data={"code": made["code"], "reply": "On our way", "key": "wrong"}).status_code == 403
    assert web.post("/desk/reply", data={"code": made["code"], "reply": "We are ready for them", "key": "desk-secret"}, follow_redirects=False).status_code == 303
    page = web.get("/?key=desk-secret").text
    assert "(0 waiting)" in page and "We are ready for them" in page and "answered" in page


# ---- what a call files --------------------------------------------------------------------------------------------------------------


def test_a_question_the_guide_does_not_cover_appears_on_the_centres_front_desk_without_the_phone_number(client, happ, hdb):
    call(client, ["ask the cancer centre about the weather on mars"])
    queue = all_requests(happ)
    assert len(queue) == 1 and queue[0]["kind"] == "question" and "weather on mars" in queue[0]["summary"] and not queue[0]["urgent"]
    assert CALLER.lstrip("+") not in queue[0]["summary"] and CALLER.lstrip("+") not in queue[0]["caller_ref"]
    row = hdb.scalar(select(HealthRequest))
    assert row.code == queue[0]["code"] and row.status == "open" and row.sms_name == "the Ikeja care centre"


def test_an_emergency_is_flagged_urgent_to_the_nearest_emergency_care_and_the_reply_is_unchanged_in_what_matters(client, happ, hdb):
    replies = call(client, ["find the nearest hospital in Ikeja, he is not breathing"])
    assert "112" in replies[1] and "I have also told" in replies[1]
    urgent = [r for r in all_requests(happ) if r["kind"] == "emergency"]
    assert len(urgent) == 1 and urgent[0]["urgent"] and "not breathing" in urgent[0]["summary"] and "Ikeja" in urgent[0]["summary"]
    assert hdb.scalar(select(HealthRequest)).urgent is True


def test_staff_are_texted_about_an_urgent_request_only_when_a_staff_number_is_set(client, happ, hdb):
    call(client, ["find the nearest hospital in Ikeja, he is not breathing"])
    assert not hdb.scalars(select(Notification).where(Notification.template == "health_staff_alert")).all()
    happ.state.svc.settings.hospital_staff_phone = STAFF
    call(client, ["find the nearest hospital in Surulere, she collapsed"], caller="+2348055550002")
    alerts = hdb.scalars(select(Notification).where(Notification.template == "health_staff_alert")).all()
    assert len(alerts) == 1 and alerts[0].to_number == STAFF and "Urgent request REQ-" in alerts[0].body
    assert "Surulere" not in alerts[0].body and "collapsed" not in alerts[0].body       # the text carries a code and a place, not what was said


def test_a_request_that_cannot_be_filed_never_costs_the_caller_the_emergency_answer(client, happ):
    happ.state.net.down = {"state-phc", "eko-care", "onc-centre"}      # only the pharmacy chain answers: no emergency care is found, and nothing can be filed
    replies = call(client, ["find the nearest hospital in Ikeja, he is not breathing"])
    assert replies[1]      # the call went on and said something


# ---- the answer comes back -------------------------------------------------------------------------------------------------------------


def test_the_staffs_answer_is_texted_to_the_caller_once(client, happ, hdb):
    call(client, ["ask the cancer centre about the weather on mars"])
    code = all_requests(happ)[0]["code"]
    run = lambda: asyncio.run(followups.run(happ.state.svc, FIXED))
    assert run()["replied"] == 0                                         # not answered yet: nothing to send
    assert happ.state.providers["onc-centre"].state.store.reply_request(code, "We do not know the weather on mars, but we are open until five.")
    assert run()["replied"] == 1
    texts = hdb.scalars(select(Notification).where(Notification.template == "health_request_reply")).all()
    assert len(texts) == 1 and texts[0].to_number == CALLER
    assert "the Ikeja care centre" in texts[0].body and code in texts[0].body and "open until five" in texts[0].body
    assert run()["replied"] == 0 and len(hdb.scalars(select(Notification).where(Notification.template == "health_request_reply")).all()) == 1
    assert hdb.scalar(select(HealthRequest)).status == "answered"


def test_an_answer_is_not_texted_in_the_night(client, happ, hdb):
    call(client, ["ask the cancer centre about the weather on mars"])
    happ.state.providers["onc-centre"].state.store.reply_request(all_requests(happ)[0]["code"], "Please call us.")
    night = FIXED.replace(hour=22)
    assert asyncio.run(followups.run(happ.state.svc, night))["replied"] == 0
    assert asyncio.run(followups.run(happ.state.svc, FIXED))["replied"] == 1      # the morning


def test_a_request_nobody_answers_is_given_up_after_a_week(client, happ, hdb):
    from datetime import datetime, timedelta, timezone

    call(client, ["ask the cancer centre about the weather on mars"])
    row = hdb.scalar(select(HealthRequest))
    row.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    hdb.commit()
    asyncio.run(followups.run(happ.state.svc, FIXED))
    hdb.expire_all()
    assert hdb.scalar(select(HealthRequest)).status == "expired"
