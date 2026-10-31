"""The website of the health build: a clear, simple story of what Talk is, with nothing left over from the earlier shopping and farm builds."""

import pytest
from fastapi.testclient import TestClient

from sofa.config import Settings
from sofa.main import create_app

PAGES = ["/overview", "/overview/technology", "/overview/use-cases", "/overview/providers", "/overview/video", "/demo"]
OLD = ["n-atlas", "agricultur", "fertili", "maize", "farm", "npk", "urea", "demobank", "shop owner", "cii store", "africa's talking", "spitch", "yinusa yakubu for the demobank"]


@pytest.fixture
def site(tmp_path):
    app = create_app(Settings(synthetic_hospital=True, services_enabled="lookup,health", demo_page_enabled=True, database_url="sqlite://", storage_dir=str(tmp_path),
                              at_callback_secret="x", public_base_url="http://test", scheduler_enabled=False, gateway_number="+2348000009999", hub_test_number="+234 805 555 0100",
                              hub_contact_email="hello@example.com", llm_provider="meta", llm_model="muse-spark-1.3", _env_file=None))
    with TestClient(app) as client:
        yield client


def test_the_front_door_goes_to_the_overview(site):
    r = site.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/overview"


def test_every_page_loads_and_none_carries_anything_from_the_earlier_builds(site):
    for path in PAGES:
        r = site.get(path)
        assert r.status_code == 200, path
        text = r.text.lower()
        for word in OLD:
            assert word not in text, f"{path} still says {word!r}"


def test_the_home_page_explains_the_problem_the_solution_and_how_to_try_it(site):
    page = site.get("/overview").text
    for line in ("essential services, from the phone people already have", "A phone is not the same as access", "Paper, handwritten registers", "Why we start with community healthcare",
                 "Digital infrastructure", "Information organisation", "AI-enabled coordination", "Clinical decisions stay with people", "You decide, not Talk", "an area is not a hospital",
                 "Test it by phone", "+234 805 555 0100", "tel:+2348055550100", "Book an appointment in Yaba", "Muse Spark", "Muse Voice Transcribe", "Partner with us", "hello@example.com"):
        assert line.lower() in page.lower(), line


def test_the_menu_and_footer_are_the_health_builds_own(site):
    page = site.get("/overview").text
    for label in ("Overview", "How it works", "Use cases", "The providers", "Live demo"):
        assert f">{label}</a>" in page, label
    assert "What we found" not in page and "Shop owner portal" not in page and "Bank service desk" not in page
    assert "Meta Global AI Developer Hackathon" in page and "simulated" in page and "/synthetic/onc-centre/" in page


def test_the_providers_page_lists_all_four_with_a_working_front_desk_each(site):
    page = site.get("/overview/providers").text
    for name in ("Lagos State Primary Health Network", "Eko Care Private Clinics", "Everwell Pharmacies", "Oncology Care Centre Ikeja"):
        assert name in page
    for slug in ("state-phc", "eko-care", "everwell", "onc-centre"):
        assert f'href="/synthetic/{slug}/"' in page and site.get(f"/synthetic/{slug}/").status_code == 200


def test_the_earlier_builds_pages_are_not_served(site):
    assert site.get("/overview/findings").status_code == 404
    assert site.get("/vendor").status_code in (404, 307, 303)     # no shop portal in this build
    assert "/vendor" not in site.get("/overview").text


def test_the_demo_page_shows_this_builds_numbers_and_scenarios(site):
    page = site.get("/demo").text
    assert "Healthcare by phone call" in page and "muse-spark-1.3" in page
    lower = page.lower()
    assert "visits booked" in lower and "reminders and follow-ups sent" in lower
    assert "Choose a clinic" in page and "Ask the cancer centre" in page and "An emergency" in page
    assert "orders placed" not in lower and "bank transfers" not in lower


def test_the_pitch_deck_is_kept_and_listed_when_it_is_there(site, tmp_path):
    folder = tmp_path / "overview" / "deck"
    folder.mkdir(parents=True)
    (folder / "01.jpg").write_bytes(b"\xff\xd8\xff")
    assert ">Pitch deck</a>" in site.get("/overview").text and site.get("/overview/deck").status_code == 200
