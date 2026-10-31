"""A caller reaches the health network through Talk. Talk asks three independent provider systems (a state health network, a private clinic group, a pharmacy
chain), puts the real options in front of the caller, and asks which one they mean: an area is not a hospital. Only then does it ask a provider for slots and book.
Nothing in Talk is written for any one provider: they sit behind the health front desk."""

from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from scripts.seed import seed
from scripts.simulate_call import run_call
from sofa.config import Settings
from sofa.gateway.adapters.health import HealthAdapter
from sofa.main import create_app
from sofa.models import Notification

SECRET, GATEWAY, CALLER = "test-secret", "+2348000009999", "+2348055550001"
OTHER = "+2348055550002"
FIXED = datetime(2026, 10, 27, 10, 15)   # a Tuesday morning in Lagos


class Providers(httpx.AsyncBaseTransport):
    """The internet between Talk and the providers: each https://health.example/synthetic/<name>/rpc goes to the provider app of that name."""

    def __init__(self, apps):
        self.apps, self.down, self.seen, self.before_book = apps, set(), [], None

    async def handle_async_request(self, request):
        slug = request.url.path.split("/")[2]
        self.seen.append((slug, request.content.decode()))
        if self.before_book and '"tool":"book"' in request.content.decode():
            hook, self.before_book = self.before_book, None
            hook()
        if slug in self.down:
            return httpx.Response(503, json={"error": "down"})
        inner = httpx.Request(request.method, request.url.copy_with(path="/rpc"), headers=request.headers, content=request.content)
        return await httpx.ASGITransport(app=self.apps[slug]).handle_async_request(inner)


@pytest.fixture
def happ(tmp_path, monkeypatch):
    app = create_app(Settings(synthetic_hospital=True, hospital_base_url="https://health.example/synthetic", services_enabled="commerce,lookup,health", database_url="sqlite://",
                              storage_dir=str(tmp_path), at_callback_secret=SECRET, public_base_url="http://test", debug_headers=True, scheduler_enabled=False,
                              gateway_number=GATEWAY, verify_random_rate=0.0, _env_file=None))
    seed(app.state.svc.session_factory)
    apps = {r.path.rsplit("/", 1)[-1]: r.app for r in app.router.routes if getattr(r, "path", "").startswith("/synthetic/")}
    for provider in apps.values():
        provider.state.store.clock = lambda: FIXED
    app.state.providers = apps
    app.state.net = Providers(apps)
    app.state.svc.connectors.use_http(httpx.AsyncClient(transport=app.state.net, base_url="https://health.example"))
    monkeypatch.setattr(HealthAdapter, "now", lambda self: FIXED)
    return app


@pytest.fixture
def client(happ):
    with TestClient(happ) as c:
        yield c


@pytest.fixture
def hdb(happ):
    with happ.state.svc.session_factory() as session:
        yield session


@pytest.fixture
def said(happ):
    """The model's reading of the caller's words, set per test: {sentence: the fields it filled in}. 'choice:<sentence>' sets which option it picks. The rest is the stand-in model."""
    readings: dict = {}
    llm = happ.state.svc.llm
    real = llm.extract

    async def extract(model, system, transcript, alternatives=None, json_schema=None):
        if system.startswith("ARGUMENTS"):
            return model(**readings.get(transcript, {})), {}
        if system.startswith("CHOICE"):
            return model(choice=readings.get("choice:" + transcript)), {}
        return await real(model, system, transcript, alternatives, json_schema)

    llm.extract = extract
    return readings


def call(client, lines, caller=CALLER):
    return run_call(client, lines, caller, GATEWAY, "en", SECRET, echo=lambda *_: None)


def appointments(happ, slug):
    return [dict(r) for r in happ.state.providers[slug].state.store.db.execute("SELECT * FROM appointments WHERE status = 'booked'").fetchall()]


# ---- how it is set up ---------------------------------------------------------------------------------------


def test_it_is_not_there_unless_switched_on(tmp_path):
    app = create_app(Settings(database_url="sqlite://", storage_dir=str(tmp_path), at_callback_secret=SECRET, public_base_url="http://test", scheduler_enabled=False,
                              gateway_number=GATEWAY, _env_file=None))
    with TestClient(app) as c:
        assert c.get("/synthetic/state-phc/").status_code == 404
    assert "health" not in app.state.svc.gateway.domains()


# ---- finding a place -------------------------------------------------------------------------------------------


def test_a_search_asks_every_provider_and_offers_a_booking(client, happ):
    replies = call(client, ["find the nearest clinic for antenatal care in Surulere", "no thanks"])
    first = replies[1]
    assert "Aguda Community Health Centre" in first and "Surulere Care Clinic" in first      # a state centre and a private clinic, from two different systems
    assert "state primary health centre" in first and "private clinic" in first and "naira" in first and "minutes" in first
    assert "Would you like me to book an appointment at one of these?" in first
    assert {slug for slug, body in happ.state.net.seen if '"search"' in body} >= {"state-phc", "eko-care"}


def test_an_emergency_gets_the_emergency_number_first_and_no_queue(client):
    replies = call(client, ["find the nearest hospital in Ikeja, he is not breathing"])
    assert replies[1].startswith("If this is an emergency, call 112 now.") and "Ikeja Heights General Hospital" in replies[1]
    assert "people are waiting" not in replies[1] and "book an appointment" not in replies[1]


def test_a_medicine_is_looked_up_at_the_pharmacy_chain(client):
    replies = call(client, ["do you have amoxicillin in stock", "amoxicillin"])
    assert "Which medicine" in replies[1]
    assert "Amoxicillin 500 milligram capsules" in replies[2] and "Everwell Pharmacy" in replies[2] and "needs a prescription" in replies[2] and "naira" in replies[2]


# ---- booking: the caller chooses the place -------------------------------------------------------------------------


def test_an_area_is_not_a_hospital_so_the_caller_is_asked_which_one(client, happ):
    replies = call(client, ["book an appointment in Surulere", "the private one", "tomorrow morning", "yes please"])
    options = replies[1]
    assert "I found 3 places near Surulere" in options and "Aguda Community Health Centre" in options and "Surulere Care Clinic" in options and "Which one would you like?" in options
    assert "What day and time would you like at Surulere Care Clinic" in replies[2]
    assert "I can book you at Surulere Care Clinic tomorrow at 8 in the morning" in replies[3] and "Shall I book it" in replies[3]
    assert "You are booked at Surulere Care Clinic tomorrow at 8 in the morning" in replies[4] and "booking code is" in replies[4] and "texted you the details" in replies[4]
    assert len(appointments(happ, "eko-care")) == 1 and appointments(happ, "state-phc") == []     # booked with the private clinic's own system, and only there


def test_the_caller_can_choose_by_order_by_price_by_name_or_by_how_busy(client, happ):
    assert "You are booked at Aguda Community Health Centre" in call(client, ["book an appointment in Surulere", "the cheaper one", "tomorrow morning", "yes"])[4]
    assert "You are booked at Surulere Care Clinic" in call(client, ["book an appointment in Surulere", "the first one", "tomorrow morning", "yes"], OTHER)[4]
    assert "What day and time would you like at Aguda Community Health Centre" in call(client, ["book an appointment in Surulere", "Aguda"], "+2348055550003")[2]
    assert "What day and time would you like at Surulere Care Clinic" in call(client, ["book an appointment in Surulere", "the quicker one"], "+2348055550004")[2]


def test_when_the_model_reads_a_named_place_it_goes_straight_to_the_offer(client, happ, said):
    said["book an appointment at Aguda tomorrow morning"] = {"facility": "Aguda", "when": "tomorrow morning"}
    replies = call(client, ["book an appointment at Aguda tomorrow morning", "yes please"])
    assert "I can book you at Aguda Community Health Centre tomorrow at 8 in the morning" in replies[1]       # no which-one, no what-time first
    assert "You are booked at Aguda Community Health Centre" in replies[2] and len(appointments(happ, "state-phc")) == 1


def test_a_vague_request_is_asked_about_one_thing_at_a_time(client):
    replies = call(client, ["book an appointment", "Surulere", "the first one", "friday afternoon", "yes"])
    assert "Which clinic, or which part of Lagos?" in replies[1] and "I found 3 places near Surulere" in replies[2]
    assert "What day and time" in replies[3] and "Friday 30 October at 12 noon" in replies[4]
    assert "You are booked" in replies[5] and "Friday 30 October" in replies[5]


def test_a_value_that_is_not_in_the_callers_words_is_dropped_and_asked_for(client, said):
    said["book me an appointment please"] = {"facility": "Aguda Community Health Centre", "when": "Friday 30 October"}   # the model made these up
    replies = call(client, ["book me an appointment please"])
    assert "Which clinic, or which part of Lagos?" in replies[1]


def test_a_no_asks_for_another_time_and_a_new_time_replaces_the_old_one(client):
    replies = call(client, ["book an appointment in Surulere", "the second one", "tomorrow morning", "no", "thursday at 2 pm", "yes"])
    assert "No problem. What day and time would suit you better?" in replies[4]
    assert "Thursday 29 October at 2 in the afternoon" in replies[5] and "You are booked" in replies[6]


def test_a_slot_taken_while_the_caller_was_deciding_is_not_booked_and_the_next_is_offered(client, happ):
    store = happ.state.providers["state-phc"].state.store

    def fill_the_slot():                      # two other patients take the offered slot while the caller thinks
        for who in ("x", "y"):
            store.book(store.by_id["hc-aguda"], datetime(2026, 10, 28, 8, 0), "general consultation", who)

    happ.state.net.before_book = fill_the_slot
    replies = call(client, ["book an appointment in Surulere", "the second one", "tomorrow morning", "yes"])
    assert "I can book you at Aguda Community Health Centre tomorrow at 8 in the morning" in replies[3]
    assert "That time was just taken" in replies[4] and "8:30" in replies[4] and "Shall I book it" in replies[4]
    assert len([a for a in appointments(happ, "state-phc") if a["caller_ref"] not in ("x", "y")]) == 0     # nothing was booked for the caller


def test_a_caller_who_cannot_choose_is_not_pushed(client):
    replies = call(client, ["book an appointment in Surulere", "hmm I am not sure", "I do not know"])
    assert "Sorry, which one?" in replies[2] and "Aguda Community Health Centre" in replies[2]
    assert "booked" not in replies[3].lower()


def test_the_model_chooses_when_the_rules_cannot(client, said):
    said["choice:the one near the market"] = "2"
    replies = call(client, ["book an appointment in Surulere", "the one near the market", "tomorrow morning", "yes"])
    assert "You are booked at" in replies[4]


# ---- status and slots -------------------------------------------------------------------------------------------------


def test_the_queue_and_hours_of_a_named_place(client, said):
    said["how busy is Aguda right now"] = {"facility": "Aguda"}
    replies = call(client, ["how busy is Aguda right now"])
    assert "Aguda Community Health Centre, a state primary health centre on Bode Thomas Street" in replies[1] and "open until 4 in the evening" in replies[1]
    assert "waiting" in replies[1] and "500 naira" in replies[1] and "It offers" in replies[1]


def test_the_free_times_at_a_named_place(client, said):
    said["what times are free at Surulere Care"] = {"facility": "Surulere Care"}
    replies = call(client, ["what times are free at Surulere Care"])
    assert "The next free times at Surulere Care Clinic are" in replies[1] and "today at" in replies[1]


# ---- cancelling ---------------------------------------------------------------------------------------------------------


def test_a_caller_cancels_their_appointment_with_whichever_provider_holds_it(client, hdb, happ):
    call(client, ["book an appointment in Surulere", "the second one", "tomorrow morning", "yes"])
    replies = call(client, ["cancel my appointment", "yes"])
    assert "Your next appointment is at Aguda Community Health Centre tomorrow at 8 in the morning" in replies[1] and "Shall I cancel it" in replies[1]
    assert "Your appointment is cancelled" in replies[2] and "texted you" in replies[2]
    assert appointments(happ, "state-phc") == []
    assert hdb.scalar(select(Notification).where(Notification.body.like("Cancelled:%")))
    assert "I do not see an appointment" in call(client, ["cancel my appointment"], OTHER)[1]


def test_a_no_keeps_the_appointment(client, happ):
    call(client, ["book an appointment in Surulere", "the second one", "tomorrow morning", "yes"])
    assert "kept your appointment" in call(client, ["cancel my appointment", "no"])[2] and len(appointments(happ, "state-phc")) == 1


# ---- what the providers see ------------------------------------------------------------------------------------------------


def test_each_provider_sees_a_different_reference_and_never_the_phone_number(client, happ):
    call(client, ["find the nearest clinic for antenatal care in Surulere", "no"])
    seen = happ.state.net.seen
    assert seen and all(CALLER not in body and CALLER.replace("+234", "0") not in body for _, body in seen)
    import json

    ids = {slug: json.loads(body)["context"]["caller_id"] for slug, body in seen if '"op":"call"' in body}
    assert len(set(ids.values())) == len(ids) >= 2


def test_a_text_confirmation_goes_only_to_the_caller_and_only_when_accepted(client, hdb):
    call(client, ["book an appointment in Surulere", "the second one", "tomorrow morning", "yes"])
    texts = hdb.scalars(select(Notification).where(Notification.template == "health_notice")).all()
    assert len(texts) == 1 and texts[0].to_number == CALLER and texts[0].body.startswith("Booked: Aguda Community Health Centre")


# ---- when a provider is down -------------------------------------------------------------------------------------------------


def test_one_provider_down_does_not_stop_the_others(client, happ):
    happ.state.net.down.add("state-phc")
    first = call(client, ["find the nearest clinic for antenatal care in Surulere"])[1]
    assert "Surulere Care Clinic" in first and "Aguda" not in first


# ---- what the caller already said is not asked again ------------------------------------------------------------------------


def test_a_clinic_search_and_a_medicine_need_no_question_when_the_caller_said_it(client, said):
    said["find the nearest clinic in Yaba"] = {"area": "Yaba"}
    assert "Yaba Mainland Health Centre" in call(client, ["find the nearest clinic in Yaba"])[1]
    said["do you have amoxicillin in stock"] = {"drug": "amoxicillin"}
    assert "Amoxicillin 500 milligram capsules" in call(client, ["do you have amoxicillin in stock"], OTHER)[1]


def test_what_the_model_understands_by_meaning_picks_the_service(client, said):
    said["my baby needs her jabs, we live in Mushin"] = {"need": "child immunisation", "area": "Mushin"}
    replies = call(client, ["find a clinic for my baby needs her jabs, we live in Mushin"])
    said["find a clinic for my baby needs her jabs, we live in Mushin"] = {"need": "child immunisation", "area": "Mushin"}
    replies = call(client, ["find a clinic for my baby needs her jabs, we live in Mushin"], OTHER)
    assert "place for child immunisation" in replies[1] and "Mushin" in replies[1]


# ---- an emergency is never answered with a menu -------------------------------------------------------------------------------


def test_an_emergency_with_no_area_gets_the_emergency_number_at_once_and_then_the_nearest_care(client):
    replies = call(client, ["please help, he is not breathing", "Ikeja"])
    assert replies[1].startswith("If this is an emergency, call 112 now.") and "Which part of Lagos are you in" in replies[1]
    assert "Ikeja Heights General Hospital" in replies[2] and "book an appointment" not in replies[2] and "people are waiting" not in replies[2]


def test_an_emergency_in_the_middle_of_something_else_takes_over(client):
    replies = call(client, ["book an appointment in Surulere", "my father has collapsed, he is unconscious, we are in Ikeja"])
    assert replies[2].startswith("If this is an emergency, call 112 now.") and "Ikeja Heights General Hospital" in replies[2]


# ---- the simulated cancer centre -----------------------------------------------------------------------------------------------------


def test_a_caller_finds_the_cancer_centre_only_when_they_need_cancer_care(client):
    replies = call(client, ["find the nearest clinic for cancer care in Ikeja", "no thanks"])
    assert "The nearest place for cancer care is Oncology Care Centre Ikeja, a state cancer centre on Oba Akran Avenue" in replies[1]
    assert "Oncology Care Centre" not in call(client, ["find the nearest clinic in Ikeja", "no thanks"], OTHER)[1]       # a specialist centre is not a general clinic


def test_a_basic_question_is_answered_from_the_centres_own_guide(client):
    replies = call(client, ["what should I bring to my first visit at the cancer centre"])
    assert "referral letter" in replies[1] and "photo ID" in replies[1]


def test_a_warning_sign_is_answered_exactly_as_the_centre_wrote_it(client):
    replies = call(client, ["ask the cancer centre what to do about a fever during chemotherapy"])
    assert "Contact the centre straight away" in replies[1] and "call 112" in replies[1]


def test_a_question_the_guide_does_not_cover_goes_to_the_care_team_not_a_guess(client, hdb):
    from sofa.models import Handoff

    replies = call(client, ["ask the cancer centre about the weather on mars"])
    assert "referral letter" not in replies[1] and "pass the details to the clinic" in replies[1]
    assert hdb.scalar(select(Handoff))


def test_a_booking_at_the_cancer_centre_is_remembered_and_its_text_never_names_what_is_treated(client, hdb, happ):
    from sofa.models import HealthAppointment

    replies = call(client, ["book an appointment at the cancer centre", "Ikeja", "tomorrow morning", "yes"])
    assert "What day and time would you like at Oncology Care Centre Ikeja" in replies[2] and "You are booked at Oncology Care Centre Ikeja tomorrow at 8 in the morning" in replies[4]
    text = hdb.scalar(select(Notification).where(Notification.template == "health_notice"))
    assert text.body.startswith("Booked: the Ikeja care centre") and "Oncology" not in text.body and "cancer" not in text.body.lower()
    row = hdb.scalar(select(HealthAppointment))
    assert row.provider == "health_onc_centre" and row.sms_name == "the Ikeja care centre" and row.status == "booked" and row.starts_local == "2026-10-28T08:00:00"
    assert appointments(happ, "onc-centre")[0]["visit"] == "cancer care"
