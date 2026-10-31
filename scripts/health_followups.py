"""Run one follow-up pass by hand: reminders for visits coming up, and thank-yous or "we missed you" texts for visits that have passed.

    python -m scripts.health_followups

The server does this by itself every few minutes; this is for a demonstration or a check. It uses the same settings as the server, so with no SMS key set the texts
are only logged, and with one set they are really sent.
"""

import asyncio

from sofa.config import get_settings
from sofa.container import build_services
from sofa.db import make_engine, make_session_factory
from sofa.health import followups
from sofa.health.links import specs_from_settings


async def main() -> None:
    s = get_settings()
    svc = build_services(s, make_session_factory(make_engine(s.database_url)))
    health = svc.gateway.adapter("health")
    if health:
        health.set_providers(specs_from_settings(s))
    print("sms:", "mock (nothing is sent)" if s.sms_is_mock else "LIVE (texts are really sent)")
    print(await followups.run(svc))


if __name__ == "__main__":
    asyncio.run(main())
