"""Reaching the provider systems on their own: a small signed HTTP client.

Talk asks each provider system (a state health network, a private clinic group, a pharmacy chain, the cancer centre) with one signed JSON message and gets
facts back as data. This module is all of that: one POST, a signature the provider checks, a time limit and plain errors.

The message:  POST <endpoint>  {"op": "call", "tool": "<remote name>", "arguments": {...}, "context": {"caller_id", "language", "call_id", "service"}}
              X-Sofa-Timestamp: <unix seconds>   X-Sofa-Signature: hex HMAC-SHA256 of  "<timestamp>.<body>"  with the secret that provider shares with Talk.
The answer:   {"status": "ok" | "error", "say": "...", "data": {...}}
"""

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from . import data, provider
from .register import DOMAIN_OF, KINDS

log = logging.getLogger("sofa.health")
TIMEOUT_SECONDS = 8.0   # a provider that does not answer in this long is treated as unreachable: the caller never loses the call over it


class ConnectorError(Exception):
    """The provider could not be reached or answered badly."""


class Unavailable(ConnectorError):
    """The provider cannot be asked at the moment."""


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str       # "health_state_phc.search"
    remote_name: str   # "search": what the provider calls it
    description: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    """One provider system as the health front desk sees it."""
    domain: str
    label: str
    endpoint_url: str
    signing_secret: str
    config: tuple = ()  # sorted (key, value) pairs: group, kind (clinics or pharmacies), owner
    tools: tuple = field(default_factory=tuple)

    def setting(self, key: str, default=None):
        return dict(self.config).get(key, default)

    def tool(self, tool_id: str | None) -> ToolSpec | None:
        return next((t for t in self.tools if t.tool_id == tool_id), None)


@dataclass
class ToolResult:
    ok: bool = True
    say: str = ""
    data: dict = field(default_factory=dict)


def sign(secret: str, stamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def caller_ref(secret: str, phone: str) -> str:
    """The caller as one provider knows them: the same every time for that provider, and no use to anyone who does not hold its secret. The provider never
    receives a phone number."""
    return hmac.new(secret.encode(), phone.encode(), hashlib.sha256).hexdigest()[:24]


class ProviderLinks:
    """Calls a provider system: `call`, and `use_http` for a network of the caller's own."""

    def __init__(self, settings=None, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(follow_redirects=False)

    def use_http(self, client: httpx.AsyncClient) -> None:
        """Send every call through this client (the tests give it a network of their own)."""
        self.http = client

    async def call(self, db, spec: ProviderSpec, tool: ToolSpec, args: dict, ctx: dict) -> ToolResult:
        context = {"caller_id": caller_ref(spec.signing_secret, ctx.get("phone", "")), "language": ctx.get("language", "en"), "call_id": ctx.get("call_id"), "service": spec.domain}
        body = json.dumps({"op": "call", "tool": tool.remote_name, "arguments": args, "context": context}, separators=(",", ":")).encode()
        stamp = str(int(time.time()))
        headers = {"Content-Type": "application/json", "X-Sofa-Timestamp": stamp, "X-Sofa-Signature": sign(spec.signing_secret, stamp, body)}
        try:
            resp = await self.http.post(spec.endpoint_url, content=body, headers=headers, timeout=TIMEOUT_SECONDS)
            resp.raise_for_status()
            answer = resp.json()
        except httpx.TimeoutException as exc:
            raise ConnectorError("the provider did not answer in time") from exc
        except httpx.HTTPStatusError as exc:
            raise ConnectorError(f"the provider answered {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError(f"the provider could not be used ({type(exc).__name__})") from exc
        if str(answer.get("status", "ok")) == "error":
            result = ToolResult(ok=False, say=str(answer.get("say") or ""), data=answer.get("data") or {})
            if not result.say:
                raise ConnectorError("the provider reported an error")
            return result
        return ToolResult(ok=True, say=str(answer.get("say") or "").strip(), data=answer.get("data") or {})


def specs_from_settings(settings) -> list[ProviderSpec]:
    """Every provider system the settings name, ready for the health front desk (`HealthAdapter.set_providers`)."""
    out = []
    for slug, network in data.NETWORKS.items():
        domain = DOMAIN_OF[slug]
        tools = tuple(ToolSpec(f"{domain}.{t['name']}", t["name"], t["description"]) for t in provider.TOOLS)
        config = tuple(sorted({"group": "health", "kind": KINDS[slug], "owner": network.owner}.items()))
        out.append(ProviderSpec(domain, network.name, settings.provider_endpoint(slug), settings.provider_secret(slug), config, tools))
    return out
