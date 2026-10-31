"""Follow-up: Talk looks after the patient before and after a visit it booked, so the clinic's book stays true and nobody is lost.

One pass, run by the scheduler (and by hand with `python -m scripts.health_followups`):

  * a visit coming up within a day gets a reminder text (once);
  * a visit whose time has passed is checked with the clinic's own system: attended, did not come, or still not marked;
  * a patient who came gets a thank-you and an offer of the next visit; one who did not gets a "we missed you, call to book a new time" (once each).

The clinic stays the source of truth: Talk changes its own record only to match what the clinic's system says. Texts go out only between 7 in the morning and 8 in the
evening, name the place by its discreet name (never by what is treated there), and say nothing about the patient's health.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from ..models import Customer, HealthAppointment, HealthRequest
from ..services.notify import send_sms
from .when import say_day, say_time

log = logging.getLogger("sofa.health")
FIRST_HOUR, LAST_HOUR = 7, 20          # texts only go out in these hours, Lagos time
REMIND_WITHIN_HOURS = 24
GIVE_UP_AFTER_DAYS = 3                  # a visit the clinic never marks is left alone after this long
REQUEST_EXPIRES_DAYS = 7                # a request the staff never answer is no longer asked about after this long


def reminder_text(row: HealthAppointment, now: datetime) -> str:
    start = datetime.fromisoformat(row.starts_local)
    return (f"Reminder: your appointment at {row.sms_name} is {say_day(start.date(), now.date())} at {say_time(start)}. Code {row.code}. "
            "Please arrive 15 minutes early. To change or cancel, call this number.")


def attended_text(row: HealthAppointment, now: datetime) -> str:
    start = datetime.fromisoformat(row.starts_local)
    return (f"Thank you for coming to {row.sms_name} on {say_day(start.date(), now.date())}. If you have questions about your visit, call this number any time. "
            "To book your next visit, call and say book an appointment.")


def reply_text(row: HealthRequest, reply: str) -> str:
    return f"The care team at {row.sms_name} replied to your request {row.code}: {reply} If you need more help, call this number."


def missed_text(row: HealthAppointment, now: datetime) -> str:
    start = datetime.fromisoformat(row.starts_local)
    return (f"We missed you at {row.sms_name} on {say_day(start.date(), now.date())}. To book a new time, call this number and say book an appointment.")


async def run(svc, now: datetime | None = None) -> dict:
    """One pass over every visit Talk booked that is not finished. `now` is Lagos time (a plain datetime); tests give their own. Returns what it did."""
    adapter = svc.gateway.adapter("health")
    done = {"reminders": 0, "attended": 0, "missed": 0, "thanked": 0, "chased": 0, "not_marked": 0, "replied": 0}
    if not adapter or not adapter.providers:
        return done
    now = now or adapter.now()
    can_text = FIRST_HOUR <= now.hour < LAST_HOUR
    stamp = datetime.now(timezone.utc)
    with svc.session_factory() as db:
        rows = list(db.scalars(select(HealthAppointment).where(HealthAppointment.status.in_(("booked", "attended", "no_show"))).order_by(HealthAppointment.starts_local)))
        for row in rows:
            customer = db.get(Customer, row.customer_id)
            if not customer:
                continue
            start = datetime.fromisoformat(row.starts_local)
            if row.status == "booked" and start > now:
                if row.reminded_at is None and (start - now).total_seconds() <= REMIND_WITHIN_HOURS * 3600 and can_text:
                    note = await send_sms(db, svc, to=customer.phone, body=reminder_text(row, now), template="health_reminder", customer_id=customer.id)
                    if note.status in ("sent", "mock_sent"):
                        row.reminded_at, done["reminders"] = stamp, done["reminders"] + 1
                continue
            if row.status == "booked":      # its time has passed: what does the clinic's own book say?
                spec = next((s for s in adapter.providers if s.domain == row.provider), None)
                ctx = SimpleNamespace(db=db, svc=svc, customer=customer, lang="en", call=None)
                answer = await adapter.provider_call(ctx, spec, "visit_status", {"code": row.code}) if spec else None
                state = (answer or {}).get("status")
                if state in ("attended", "no_show", "cancelled"):
                    row.status = state
                    if state in ("attended", "no_show"):
                        done["attended" if state == "attended" else "missed"] += 1
                else:
                    if (now - start).days >= GIVE_UP_AFTER_DAYS:
                        done["not_marked"] += 1
                    continue
            if row.status in ("attended", "no_show") and row.followed_up_at is None and can_text:
                body = attended_text(row, now) if row.status == "attended" else missed_text(row, now)
                template = "health_thanks" if row.status == "attended" else "health_missed"
                note = await send_sms(db, svc, to=customer.phone, body=body, template=template, customer_id=customer.id)
                if note.status in ("sent", "mock_sent"):
                    row.followed_up_at = stamp
                    done["thanked" if row.status == "attended" else "chased"] += 1
        for req in list(db.scalars(select(HealthRequest).where(HealthRequest.status == "open"))):    # a request the provider's staff have answered: the answer is texted back, once
            created = req.created_at if req.created_at.tzinfo else req.created_at.replace(tzinfo=timezone.utc)
            if (stamp - created).days >= REQUEST_EXPIRES_DAYS:
                req.status = "expired"
                continue
            customer = db.get(Customer, req.customer_id)
            spec = next((s for s in adapter.providers if s.domain == req.provider), None)
            if not customer or not spec or not can_text:
                continue
            ctx = SimpleNamespace(db=db, svc=svc, customer=customer, lang="en", call=None)
            found = ((await adapter.provider_call(ctx, spec, "request_status", {"code": req.code})) or {}).get("request") or {}
            if found.get("status") == "answered" and found.get("reply"):
                note = await send_sms(db, svc, to=customer.phone, body=reply_text(req, found["reply"]), template="health_request_reply", customer_id=customer.id)
                if note.status in ("sent", "mock_sent"):
                    req.status, req.texted_at, done["replied"] = "answered", stamp, done["replied"] + 1
        db.commit()
    if any(done.values()):
        log.info("health follow-up: %s", ", ".join(f"{k} {v}" for k, v in done.items() if v))
    return done
