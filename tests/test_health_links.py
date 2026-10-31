"""The provider systems reached with a small signed client: a signed message the providers accept, plain errors, and the specs the health front desk reads."""

import asyncio

import httpx
import pytest

from sofa.config import Settings
from sofa.health import provider, register
from sofa.health.links import ConnectorError, ProviderLinks, ProviderSpec, ToolSpec, caller_ref, sign, specs_from_settings

PHONE = "+2348055550123"


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, storage_dir=str(tmp_path), at_callback_secret="test-secret", hospital_base_url="https://health.example/synthetic", synthetic_hospital=True)


class Network(httpx.AsyncBaseTransport):
    """The internet between Talk and the providers: https://health.example/synthetic/<name>/rpc reaches the provider app of that name."""

    def __init__(self, apps):
        self.apps, self.seen, self.fail = apps, [], None

    async def handle_async_request(self, request):
        self.seen.append(request)
        if self.fail:
            return self.fail(request)
        slug = request.url.path.split("/")[2]
        inner = httpx.Request(request.method, request.url.copy_with(path="/rpc"), headers=request.headers, content=request.content)
        return await httpx.ASGITransport(app=self.apps[slug]).handle_async_request(inner)


@pytest.fixture
def world(settings):
    apps = register.create_apps(settings)
    net = Network(apps)
    links = ProviderLinks(settings)
    links.use_http(httpx.AsyncClient(transport=net, base_url="https://health.example"))
    return links, {s.domain: s for s in specs_from_settings(settings)}, net


def call(links, spec, op, args=None, phone=PHONE):
    return asyncio.run(links.call(None, spec, spec.tool(f"{spec.domain}.{op}"), args or {}, {"phone": phone, "language": "en", "call_id": "c1"}))


def test_there_is_a_spec_for_each_provider_system_with_its_tools_and_kind(settings):
    specs = {s.domain: s for s in specs_from_settings(settings)}
    assert set(specs) == {"health_state_phc", "health_eko_care", "health_everwell", "health_onc_centre"}
    assert specs["health_everwell"].setting("kind") == "pharmacies" and specs["health_state_phc"].setting("kind") == "clinics"
    assert specs["health_state_phc"].setting("group") == "health"
    search = specs["health_state_phc"].tool("health_state_phc.search")
    assert search.remote_name == "search" and specs["health_state_phc"].endpoint_url == "https://health.example/synthetic/state-phc/rpc"
    assert specs["health_state_phc"].tool("health_state_phc.nothing") is None
    assert {t.remote_name for t in specs["health_state_phc"].tools} == {t["name"] for t in provider.TOOLS}


def test_each_provider_has_a_secret_of_its_own(settings):
    secrets = {s.signing_secret for s in specs_from_settings(settings)}
    assert len(secrets) == 4


def test_a_signed_call_is_accepted_by_the_provider_and_answered_with_facts(world):
    links, specs, net = world
    result = call(links, specs["health_state_phc"], "search", {"need": "antenatal care", "lat": 6.5, "lon": 3.35, "limit": 3})
    assert result.ok and result.data["facilities"]
    sent = net.seen[0]
    assert sent.headers["x-sofa-signature"] == sign(specs["health_state_phc"].signing_secret, sent.headers["x-sofa-timestamp"], sent.content)


def test_the_provider_receives_a_reference_for_the_caller_and_never_the_phone_number(world):
    links, specs, net = world
    call(links, specs["health_state_phc"], "upcoming")
    body = net.seen[0].content.decode()
    assert PHONE not in body and PHONE.lstrip("+") not in body
    assert caller_ref(specs["health_state_phc"].signing_secret, PHONE) in body
    assert caller_ref("another secret", PHONE) != caller_ref(specs["health_state_phc"].signing_secret, PHONE)  # a different provider sees a different reference


def test_a_message_signed_with_the_wrong_secret_is_refused(world):
    links, specs, _ = world
    wrong = ProviderSpec(specs["health_eko_care"].domain, "x", specs["health_eko_care"].endpoint_url, "not the secret", (), specs["health_eko_care"].tools)
    with pytest.raises(ConnectorError, match="401"):
        call(links, wrong, "search")


def test_a_provider_that_is_down_is_a_plain_error_not_a_crash(world):
    links, specs, net = world
    net.fail = lambda request: httpx.Response(503, json={"error": "down"})
    with pytest.raises(ConnectorError, match="503"):
        call(links, specs["health_state_phc"], "search")


def test_a_provider_that_does_not_answer_in_time_is_a_plain_error(world):
    links, specs, net = world

    def too_slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    net.fail = too_slow
    with pytest.raises(ConnectorError, match="in time"):
        call(links, specs["health_state_phc"], "search")


def test_an_answer_that_is_not_json_is_a_plain_error(world):
    links, specs, net = world
    net.fail = lambda request: httpx.Response(200, content=b"<html>not json</html>")
    with pytest.raises(ConnectorError, match="could not be used"):
        call(links, specs["health_state_phc"], "search")


def test_a_refusal_with_wording_is_returned_and_one_without_is_an_error(world):
    links, specs, net = world
    net.fail = lambda request: httpx.Response(200, json={"status": "error", "say": "That time is taken.", "data": {"reason": "taken"}})
    refused = call(links, specs["health_state_phc"], "book", {"facility_id": "x", "start": "later"})
    assert refused.ok is False and refused.say == "That time is taken." and refused.data == {"reason": "taken"}
    net.fail = lambda request: httpx.Response(200, json={"status": "error"})
    with pytest.raises(ConnectorError):
        call(links, specs["health_state_phc"], "book", {"facility_id": "x", "start": "later"})


def test_a_tool_the_provider_does_not_have_is_found_missing_before_any_call():
    spec = ProviderSpec("health_x", "X", "https://x.example/rpc", "s", (), (ToolSpec("health_x.search", "search"),))
    assert spec.tool("health_x.book") is None and spec.tool("health_x.search").remote_name == "search"
