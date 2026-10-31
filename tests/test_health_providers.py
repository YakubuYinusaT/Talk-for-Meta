"""The synthetic provider systems: three independent ones (a state health network, a private clinic group, a pharmacy chain), each answering only from its own
records, only to a correctly signed request, and seeing callers only as a reference."""

import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from sofa.health import data
from sofa.health.provider import Provider, create_provider_app, sign
from sofa.health.store import HospitalStore
from sofa.health.when import answer_of, parse_when

TUESDAY_10AM = datetime(2026, 10, 27, 10, 15)
CALLER = {"caller_id": "caller-aaaa"}
SURULERE = {"lat": data.AREAS["Surulere"].lat, "lon": data.AREAS["Surulere"].lon}


@pytest.fixture
def clock():
    return {"t": TUESDAY_10AM}


def make(network, clock):
    store = HospitalStore(clock=lambda: clock["t"], facilities=network.facilities)
    return Provider(network, store), store


@pytest.fixture
def state(clock):
    return make(data.STATE, clock)


@pytest.fixture
def private(clock):
    return make(data.PRIVATE, clock)


@pytest.fixture
def pharmacy(clock):
    return make(data.PHARMACIES, clock)


# ---- days and times ---------------------------------------------------------------------------------------------


def test_days_and_times_are_read_the_way_people_say_them():
    now = TUESDAY_10AM
    assert parse_when("tomorrow morning", now) == {"day": datetime(2026, 10, 28).date(), "window": (420, 720), "at": None}
    assert parse_when("friday at 2 pm", now)["day"] == datetime(2026, 10, 30).date() and parse_when("friday at 2 pm", now)["at"] == 14 * 60
    assert parse_when("at 9:30", now)["at"] == 9 * 60 + 30 and parse_when("at 3", now)["at"] == 15 * 60   # a clinic is not open at 3 in the morning
    assert parse_when("tuesday", now)["day"] == datetime(2026, 11, 3).date()                              # the next Tuesday, never today
    assert parse_when("whenever", now) == {"day": None, "window": None, "at": None}


def test_yes_and_no_are_told_apart_and_a_no_wins():
    assert answer_of("yes please") == "yes" and answer_of("no") == "no" and answer_of("no, please change it") == "no" and answer_of("blue") is None


# ---- each provider knows only its own places -----------------------------------------------------------------------


def test_each_provider_answers_only_from_its_own_places(state, private, pharmacy):
    def ids(p):
        return {f["id"] for f in p[0].search({**SURULERE, "limit": 10}, CALLER)["data"]["facilities"]}

    assert "hc-aguda" in ids(state) and "cl-surulere-care" not in ids(state)
    assert "cl-surulere-care" in ids(private) and "hc-aguda" not in ids(private)
    assert ids(pharmacy) <= {f.id for f in data.PHARMACIES.facilities} and not ids(pharmacy) & ids(state)


def test_a_search_gives_the_facts_a_caller_chooses_by(state):
    first = state[0].search({**SURULERE, "need": "antenatal care", "limit": 1}, CALLER)["data"]["facilities"][0]
    assert first["name"] == "Aguda Community Health Centre" and first["owner"] == "state" and first["fee"] == 500 and first["bookable"] is True
    assert first["open_now"] is True and first["closes"] == 16 and first["km"] < 1 and first["wait_minutes"] > 0


def test_a_private_clinic_is_dearer_and_quicker_than_a_state_centre(state, private):
    s = state[0].search({**SURULERE, "need": "antenatal care", "limit": 1}, CALLER)["data"]["facilities"][0]
    p = private[0].search({**SURULERE, "need": "antenatal care", "limit": 1}, CALLER)["data"]["facilities"][0]
    assert p["fee"] > s["fee"] and p["wait_minutes"] < s["wait_minutes"]


def test_a_search_can_be_by_name_by_need_or_by_id(state, private):
    assert [f["name"] for f in private[0].search({"query": "Surulere Care"}, CALLER)["data"]["facilities"]] == ["Surulere Care Clinic"]
    assert [f["id"] for f in state[0].search({"query": "Aguda"}, CALLER)["data"]["facilities"]] == ["hc-aguda"]
    assert state[0].search({"query": "Nowhere Special"}, CALLER)["data"]["facilities"] == []
    assert all("emergency care" in f["services"] for f in state[0].search({"need": "emergency care", "limit": 10}, CALLER)["data"]["facilities"])
    assert [f["id"] for f in state[0].search({"ids": ["hc-mushin-unity"]}, CALLER)["data"]["facilities"]] == ["hc-mushin-unity"]


def test_a_closed_place_says_when_it_opens(state, clock):
    clock["t"] = datetime(2026, 10, 31, 20, 0)    # Saturday evening
    f = state[0].search({"query": "Aguda"}, CALLER)["data"]["facilities"][0]
    assert f["open_now"] is False and f["opens_next"] == {"day": "2026-11-02", "hour": 8} and f["waiting"] == 0


# ---- booking ----------------------------------------------------------------------------------------------------------


def book(provider, facility_id, start, caller="caller-aaaa"):
    return provider.book({"facility_id": facility_id, "start": start, "visit": "antenatal care"}, {"caller_id": caller})


def test_a_slot_is_offered_and_booked_and_the_caller_can_see_it(state):
    slot = state[0].slots({"facility_id": "hc-aguda", "day": "2026-10-28", "window": [420, 720]}, CALLER)["data"]["slots"][0]
    assert slot == "2026-10-28T08:00:00"
    made = book(state[0], "hc-aguda", slot)
    assert made["status"] == "ok" and made["data"]["appointment"]["code"].startswith("APT-")
    mine = state[0].upcoming({}, CALLER)["data"]["appointments"]
    assert len(mine) == 1 and mine[0]["facility_name"] == "Aguda Community Health Centre"


def test_two_patients_fill_a_slot_and_the_third_is_refused(state):
    slot = "2026-10-28T08:00:00"
    assert book(state[0], "hc-aguda", slot, "a")["status"] == "ok" and book(state[0], "hc-aguda", slot, "b")["status"] == "ok"
    refused = book(state[0], "hc-aguda", slot, "c")
    assert refused["status"] == "error" and refused["data"]["reason"] == "taken" and refused["say"] == "taken"
    assert state[0].slots({"facility_id": "hc-aguda", "day": "2026-10-28", "count": 1}, CALLER)["data"]["slots"][0] == "2026-10-28T08:30:00"


def test_a_caller_cannot_double_book_the_same_time_or_book_the_past(state):
    assert book(state[0], "hc-aguda", "2026-10-28T09:00:00")["status"] == "ok"
    assert book(state[0], "hc-aguda", "2026-10-28T09:00:00")["data"]["reason"] == "taken"
    assert book(state[0], "hc-aguda", "2026-10-26T09:00:00")["data"]["reason"] == "slot_not_offered"


def test_a_pharmacy_cannot_be_booked(pharmacy):
    assert pharmacy[0].book({"facility_id": "ph-everwell-surulere", "start": "2026-10-28T09:00:00"}, CALLER)["data"]["reason"] == "unknown_facility"
    assert pharmacy[0].slots({"facility_id": "ph-everwell-surulere"}, CALLER)["data"]["slots"] == []


def test_only_the_caller_can_cancel_their_own_appointment(state):
    code = book(state[0], "hc-aguda", "2026-10-28T09:00:00")["data"]["appointment"]["code"]
    assert state[0].cancel({"code": code}, {"caller_id": "someone-else"})["data"]["reason"] == "not_found"
    assert len(state[0].upcoming({}, CALLER)["data"]["appointments"]) == 1
    assert state[0].cancel({"code": code}, CALLER)["status"] == "ok" and state[0].upcoming({}, CALLER)["data"]["appointments"] == []


def test_an_appointment_with_one_provider_is_invisible_to_another(state, private):
    book(state[0], "hc-aguda", "2026-10-28T09:00:00")
    assert private[0].upcoming({}, CALLER)["data"]["appointments"] == []


def test_the_queue_is_made_up_but_steady_and_the_desk_can_set_it(state, clock):
    store = state[1]
    f = store.by_id["hc-aguda"]
    assert store.queue(f, clock["t"]) == store.queue(f, clock["t"])
    store.set_queue("hc-aguda", 3)
    assert store.queue(f, clock["t"]) == (3, 20)
    clock["t"] = datetime(2026, 10, 27, 14, 30)      # over two hours later: the desk's figure has gone stale
    assert store.queue(f, clock["t"])[0] != 3


# ---- medicines ---------------------------------------------------------------------------------------------------------


def test_a_medicine_is_found_even_when_said_loosely_and_the_stock_is_the_pharmacy_chains(pharmacy):
    out = pharmacy[0].stock({"drug": "do you have coartem", **SURULERE}, CALLER)["data"]
    assert out["drug"]["name"].startswith("Artemether") and out["drug"]["prescription"] is True and out["pharmacies"]
    assert all(p["kind"] == "pharmacy" and p["qty"] > 0 and p["price"] == out["drug"]["price"] for p in out["pharmacies"])
    assert pharmacy[0].stock({"drug": "zzzz"}, CALLER)["data"] == {"drug": None, "pharmacies": []}


def test_stock_is_fixed_and_some_things_are_out_of_stock():
    levels = [data.stock_level(f.id, d.id) for f in data.PHARMACIES.facilities for d in data.DRUGS]
    assert levels == [data.stock_level(f.id, d.id) for f in data.PHARMACIES.facilities for d in data.DRUGS] and 0 in levels and max(levels) > 0


# ---- the connector: signed requests only, and no phone numbers -------------------------------------------------------


def web_for(network, secret, clock):
    store = HospitalStore(clock=lambda: clock["t"], facilities=network.facilities)
    return TestClient(create_provider_app(network, secret, store)), store


def signed(payload, secret, stamp=None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    stamp = stamp or str(int(time.time()))
    return {"content": body, "headers": {"X-Sofa-Timestamp": stamp, "X-Sofa-Signature": sign(secret, stamp, body), "Content-Type": "application/json"}}


def test_a_provider_answers_only_a_correctly_signed_and_fresh_request(clock):
    web, _ = web_for(data.STATE, "state-secret", clock)
    assert web.post("/rpc", **signed({"op": "list_tools"}, "state-secret")).json()["tools"][0]["name"] == "search"
    assert web.post("/rpc", **signed({"op": "list_tools"}, "another-providers-secret")).status_code == 401
    assert web.post("/rpc", **signed({"op": "list_tools"}, "state-secret", stamp=str(int(time.time()) - 3600))).status_code == 401   # an old message cannot be sent again
    assert web.post("/rpc", json={"op": "list_tools"}).status_code == 401


def test_a_call_over_the_connector_returns_facts_not_sentences(clock):
    web, _ = web_for(data.PRIVATE, "p", clock)
    out = web.post("/rpc", **signed({"op": "call", "tool": "search", "arguments": {**SURULERE, "limit": 1}, "context": {"caller_id": "c1"}}, "p")).json()
    assert out["status"] == "ok" and out["data"]["facilities"][0]["name"] == "Surulere Care Clinic"
    assert web.post("/rpc", **signed({"op": "call", "tool": "no_such_tool", "arguments": {}, "context": {}}, "p")).json()["status"] == "error"
    assert web.post("/rpc", **signed({"op": "call", "tool": "slots", "arguments": {"facility_id": "x"}, "context": {}}, "p")).json()["data"]["reason"] == "unknown_facility"


def test_the_front_desk_page_is_labelled_synthetic_and_shows_callers_only_as_a_short_reference(clock):
    web, store = web_for(data.STATE, "s", clock)
    made = Provider(data.STATE, store).book({"facility_id": "hc-aguda", "start": "2026-10-28T09:00:00"}, {"caller_id": "0123456789abcdef0123"})
    page = web.get("/").text
    assert "Synthetic data" in page and "Lagos State Primary Health Network" in page and "Aguda Community Health Centre" in page and "never sees a phone number" in page
    assert made["data"]["appointment"]["code"] in page and "01234567" in page and "0123456789abcdef0123" not in page


# ---- the simulated cancer centre: a fixed time for every patient, a patient guide, and a record of who came ------------------------------


@pytest.fixture
def centre(clock):
    return make(data.CANCER, clock)


def test_the_cancer_centre_gives_every_patient_a_fixed_time(centre):
    slot = "2026-10-28T09:00:00"
    assert centre[0].book({"facility_id": "onc-ikeja", "start": slot, "visit": "cancer care"}, {"caller_id": "a"})["status"] == "ok"
    assert centre[0].book({"facility_id": "onc-ikeja", "start": slot, "visit": "cancer care"}, {"caller_id": "b"})["data"]["reason"] == "taken"    # one patient to a time
    assert centre[0].slots({"facility_id": "onc-ikeja", "day": "2026-10-28", "window": [480, 600], "count": 3}, CALLER)["data"]["slots"][:2] == ["2026-10-28T08:00:00", "2026-10-28T08:30:00"]


def test_a_specialist_centre_is_offered_for_its_own_need_or_by_name_never_as_a_general_clinic(centre, clock):
    ikeja = {"lat": data.AREAS["Ikeja"].lat, "lon": data.AREAS["Ikeja"].lon}
    assert centre[0].search({**ikeja}, CALLER)["data"]["facilities"] == []
    assert [f["id"] for f in centre[0].search({**ikeja, "need": "cancer care"}, CALLER)["data"]["facilities"]] == ["onc-ikeja"]
    assert [f["id"] for f in centre[0].search({"query": "Oncology Care Centre"}, CALLER)["data"]["facilities"]] == ["onc-ikeja"]
    assert centre[0].search({"need": "cancer care"}, CALLER)["data"]["facilities"][0]["sms_name"] == "the Ikeja care centre"      # a text never names what is treated


def test_the_patient_guide_answers_from_its_entries_only(centre, state):
    out = centre[0].knowledge({"query": "what should I bring to my first visit"}, CALLER)["data"]["entries"]
    assert out[0]["id"] == "first-visit" and "referral letter" in out[0]["answer"] and out[0]["source"].endswith("(sample content)")
    assert centre[0].knowledge({"query": "what is the weather on mars"}, CALLER)["data"]["entries"] == []
    assert state[0].knowledge({"query": "what should I bring to my first visit"}, CALLER)["data"]["entries"] == []         # only a provider with a guide has one


def test_a_warning_sign_always_gets_the_urgent_entry_first(centre):
    out = centre[0].knowledge({"query": "I have a fever after chemotherapy, what should I do"}, CALLER)["data"]["entries"]
    assert out[0]["id"] == "urgent-signs" and "112" in out[0]["answer"]


def test_the_desk_marks_who_came_and_the_patient_can_ask_what_became_of_a_visit(centre):
    code = centre[0].book({"facility_id": "onc-ikeja", "start": "2026-10-28T09:00:00"}, CALLER)["data"]["appointment"]["code"]
    assert centre[0].visit_status({"code": code}, CALLER)["data"]["status"] == "booked"
    assert centre[0].visit_status({"code": code}, {"caller_id": "someone-else"})["data"]["reason"] == "not_found"     # another caller's visit is not theirs to see
    assert centre[1].mark(code, "attended") is True and centre[1].mark(code, "attended") is False and centre[1].mark(code, "nonsense") is False
    assert centre[0].visit_status({"code": code}, CALLER)["data"]["status"] == "attended"
    assert centre[0].upcoming({}, CALLER)["data"]["appointments"] == []          # a visit that has been attended is no longer coming up


def test_the_desk_page_marks_visits_only_with_the_desk_key(clock):
    store = HospitalStore(clock=lambda: clock["t"], facilities=data.CANCER.facilities)
    web = TestClient(create_provider_app(data.CANCER, "s", store, desk_key="desk-secret"))
    code = Provider(data.CANCER, store).book({"facility_id": "onc-ikeja", "start": "2026-10-28T09:00:00"}, CALLER)["data"]["appointment"]["code"]
    assert "Did not come" not in web.get("/").text and "Sample content" in web.get("/").text                 # no buttons without the key, and the guide is labelled sample
    assert "Did not come" in web.get("/?key=desk-secret").text
    assert web.post("/desk/mark", data={"code": code, "outcome": "attended", "key": "wrong"}).status_code == 403
    assert store.visit(code, "caller-aaaa")["status"] == "booked"
    assert web.post("/desk/mark", data={"code": code, "outcome": "attended", "key": "desk-secret"}, follow_redirects=False).status_code == 303
    assert store.visit(code, "caller-aaaa")["status"] == "attended" and "attended" in web.get("/?key=desk-secret").text
    assert TestClient(create_provider_app(data.CANCER, "s", store)).post("/desk/mark", data={"code": code, "outcome": "no_show", "key": ""}).status_code == 403   # no key set: read only
