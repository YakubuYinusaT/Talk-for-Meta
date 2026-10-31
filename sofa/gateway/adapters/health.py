"""The health front desk: Talk coordinating several independent provider systems for one caller.

A caller says what they need. Talk works out the need and the place (the model reads the words, and what it is given must be made of them), asks every
provider system that could help, puts the real options in front of the caller with the things that decide between them (distance, waiting time, fee), and
asks which one they mean: an area is not a hospital, so Talk never picks for them. Only then does it ask a provider for slots and book. Every provider stays
the source of truth for its own records; Talk keeps the conversation, and says only what a provider confirmed.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select

from ...dialogue.manager import Outcome
from ...health import gazetteer, speak
from ...health.data import AREAS
from ...health.when import answer_of, join_names, lagos_now, parse_when, said_nothing_about_time, say_day, say_time
from ...health.links import ConnectorError, Unavailable
from ..tools import Tool
from . import ServiceAdapter

log = logging.getLogger("sofa.health")
DOMAIN = "health"
PENDING = "partner_health"             # the gateway routes every pending question of this kind back here
MAX_OPTIONS, MAX_TRIES = 3, 2
ORDINALS = {"first": 0, "1st": 0, "1": 0, "second": 1, "2nd": 1, "2": 1, "third": 2, "3rd": 2, "3": 2}   # not "one": "the private one" is not the first
ORDINAL_WORDS = ("first", "second", "third")
KIND_WORDS = {"private": "private", "state": "state", "government": "state", "public": "state", "pharmacy": "pharmacy"}
PLACE_WORDS = {"clinic", "hospital", "centre", "center", "health", "community", "medical", "general", "pharmacy"}
YES_OPENERS = ("yes", "yeah", "yep", "yup", "ok", "okay", "sure", "correct")

TOOLS = (
    ("health.find_facility", "Find the nearest clinic, hospital or pharmacy for a need such as antenatal care, with its opening hours, how long the wait is and what it costs"),
    ("health.facility_status", "How busy a named clinic or hospital is right now, and when it is open"),
    ("health.available_slots", "The free appointment times at a named clinic or hospital"),
    ("health.book_appointment", "Book an appointment at a clinic or hospital"),
    ("health.cancel_appointment", "Cancel the caller's own appointment"),
    ("health.drug_availability", "Which pharmacies have a medicine in stock, and what it costs"),
    ("health.ask_the_centre", "Answer a basic question about cancer care or the cancer centre, such as what to bring, how appointments and treatment days work, or who to call, from the centre's own patient guide"),
)
QUESTIONS = {
    "health.find_facility": "Which part of Lagos are you in? For example Surulere, Yaba or Ikeja.",
    "health.facility_status": "Which clinic or hospital?",
    "health.available_slots": "Which clinic or hospital?",
    "health.book_appointment": "Which clinic, or which part of Lagos?",
    "health.drug_availability": "Which medicine? Say the name as it is written on the pack.",
}
GIVE_UP = {
    "health.find_facility": "I could not place that area. Tell me a well known place near you, such as a market or a junction, when you call again.",
    "health.drug_availability": "I could not tell which medicine that is. Say the name as it is written on the pack, when you call again.",
}
GIVE_UP_PLACE = "I could not find that place. Please tell me the name of a clinic, or a well known place near you, when you call again."


@dataclass
class Request:
    """What the caller asked for, in the shape providers need. Every field is optional: whatever was not said stays empty and is asked for."""
    need: str | None = None
    area: str | None = None       # a canonical area name
    facility: str | None = None   # a place's name, as said
    when: str | None = None       # a day and time, as said
    drug: str | None = None
    red_flag: bool = False
    tries: int = 0                # how many times the same question has been asked
    utterance: str = ""

    def pack(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "", False, 0)}

    @staticmethod
    def unpack(data: dict) -> "Request":
        return Request(**{k: v for k, v in (data or {}).items() if k in Request.__dataclass_fields__})


class Reading(BaseModel):
    need: str | None = None
    area: str | None = None
    facility: str | None = None
    when: str | None = None
    drug: str | None = None


class PickedOption(BaseModel):
    choice: str | None = None


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


class HealthAdapter(ServiceAdapter):
    domain = DOMAIN

    def __init__(self):
        self.providers: list = []     # the provider systems behind the front desk, set at start-up

    def set_providers(self, specs: list) -> bool:
        changed = list(specs) != self.providers
        self.providers = list(specs)
        return changed

    def now(self) -> datetime:
        return lagos_now()

    def tools(self) -> list[Tool]:
        return [Tool(tool_id, DOMAIN, description, "informational") for tool_id, description in TOOLS]

    def holds_session(self, st: dict) -> bool:
        return st.get("domain") == DOMAIN

    # ---- asking the providers ------------------------------------------------------------------------

    def specs(self, kind: str | None = None) -> list:
        return [s for s in self.providers if kind is None or s.setting("kind") == kind]

    def spec_of(self, place: dict):
        return next((s for s in self.providers if s.domain == place["d"]), None)

    async def provider_call(self, gw, spec, op: str, args: dict) -> dict | None:
        """One provider, one operation. The provider's facts, or None when it could not be reached (the caller never loses the call over it)."""
        tool = spec.tool(f"{spec.domain}.{op}") if spec else None
        if not tool:
            return None
        ctx = {"phone": gw.customer.phone, "name": gw.customer.name, "language": gw.lang, "call_id": str(gw.call.id) if gw.call and gw.call.id else None}
        try:
            result = await gw.svc.connectors.call(gw.db, spec, tool, args, ctx)
        except (ConnectorError, Unavailable) as exc:
            log.warning("provider %s could not be asked %s: %s", spec.domain, op, exc)
            return None
        finally:
            gw.db.commit()  # the call's record is saved at once: a database lock must not be held while the next slow step runs
        return {**result.data, "ok": result.ok}

    async def ask_all(self, gw, op: str, args: dict, kind: str | None = None) -> list[tuple[object, dict]]:
        specs = self.specs(kind)
        answers = await asyncio.gather(*(self.provider_call(gw, s, op, args) for s in specs))
        return [(s, a) for s, a in zip(specs, answers) if a is not None]

    async def places(self, gw, req: Request, *, kind: str | None, bookable: bool = False, query: str | None = None, limit: int = 4) -> tuple[list[dict], bool]:
        """Places from every provider that fits, open and nearest first. The flag is False when no provider could be reached at all."""
        args = {"limit": limit, **({"need": req.need} if req.need else {}), **({"query": query} if query else {}), **({"bookable": True} if bookable else {})}
        if req.area:
            args.update(lat=AREAS[req.area].lat, lon=AREAS[req.area].lon)
        answered = await self.ask_all(gw, "search", args, kind)
        found = [{**f, "d": spec.domain} for spec, a in answered for f in a.get("facilities", [])]
        if query and found:     # each provider names its own best match: only the best across all of them is what the caller meant
            best = max(f.get("match", 0) for f in found)
            found = [f for f in found if f.get("match", 0) >= best - 0.01]
        found.sort(key=lambda f: (not (f["open_now"] or f["all_day"] or (req.need == "emergency care" and f["emergency"])), f["km"] if f["km"] is not None else 99, f["wait_minutes"]))
        return found, bool(answered)

    async def fetch_place(self, gw, req: Request, place: dict) -> dict | None:
        """The full, current facts about one place a caller chose, from the provider that runs it."""
        args = {"ids": [place["id"]], "limit": 1, **({"lat": AREAS[req.area].lat, "lon": AREAS[req.area].lon} if req.area else {})}
        answer = await self.provider_call(gw, self.spec_of(place), "search", args)
        found = (answer or {}).get("facilities", [])
        return found[0] if found else None

    # ---- reading the caller's words ------------------------------------------------------------------

    async def read(self, gw, text: str, alternatives: list[str] | None = None) -> Request:
        """Rules first, then the model by meaning ("my baby needs jabs" is child immunisation). A place, a time or a medicine only counts if it is made of the
        caller's own words; the need is one of a fixed list. The model never supplies a fact."""
        found = parse_when(text, self.now())
        area = gazetteer.match_area(text)
        req = Request(need=gazetteer.detect_need(text), area=area.name if area else None, when=text if not said_nothing_about_time(found) else None,
                      red_flag=gazetteer.has_red_flag(text), utterance=text[:300])
        schema = {"type": "object", "additionalProperties": False, "required": list(Reading.model_fields),
                  "properties": {"need": {"anyOf": [{"type": "string", "enum": gazetteer.NEED_CHOICES}, {"type": "null"}], "description": "what the caller needs care for"},
                                 **{n: {"anyOf": [{"type": "string"}, {"type": "null"}], "description": d} for n, d in (
                                     ("area", "the part of Lagos the caller is in or means"), ("facility", "the name of a clinic, hospital or pharmacy the caller named"),
                                     ("when", "the day and time the caller asked for"), ("drug", "the medicine the caller named"))}}}
        system = ("ARGUMENTS\nA caller to a health line in Lagos said the words below. Fill in each field only if the caller actually said it, in the caller's own words; "
                  "null for anything not said. Do not guess, do not work out dates, do not add words. For need, pick the one that fits what they describe, or null.")
        try:
            got, _raw = await gw.svc.llm.extract(Reading, system, text, alternatives, json_schema=schema)
        except Exception as exc:  # the model not answering costs the caller nothing: the rules above and the questions below carry on
            log.warning("health request not read by the model: %s", exc)
            return req
        said = set(tokens(" ".join([text, *(alternatives or [])])))

        def grounded(value) -> str | None:
            return value.strip() if isinstance(value, str) and value.strip() and len(value) <= 120 and set(tokens(value)) <= said else None

        if got.need in gazetteer.NEED_CHOICES:
            req.need = got.need
        named = gazetteer.match_area(grounded(got.area) or "")
        req.area = named.name if named else req.area
        req.facility = grounded(got.facility) or req.facility
        req.when = grounded(got.when) or req.when
        req.drug = grounded(got.drug) or req.drug
        return req

    # ---- entry ----------------------------------------------------------------------------------------

    @staticmethod
    def is_emergency(text: str) -> bool:
        return gazetteer.has_red_flag(text)

    async def turn(self, gw, transcript: str, confidence: float, alternatives: list[str] | None) -> Outcome:
        if self.is_emergency(transcript):   # no model call and no menu: an emergency is answered at once
            area = gazetteer.match_area(transcript)
            return await self.start(gw, "health.find_facility", Request(need="emergency care", area=area.name if area else None, red_flag=True, utterance=transcript[:300]))
        u = await gw.understand(transcript, alternatives)
        tool_id = u.tool if u.tool in dict(TOOLS) else None
        started, gw._health_read = getattr(gw, "_health_read", None), None
        req = await started if started is not None else await self.read(gw, transcript, alternatives)    # already running since the turn began, when the gateway could start it
        if req.need == "emergency care":    # the model read an emergency by meaning
            return await self.start(gw, "health.find_facility", req)
        if not tool_id:
            options = "; ".join(description for _, description in TOOLS[:4])
            return gw.ask("partner_ask_what", {"kind": PENDING, "domain": DOMAIN, "step": "what", "req": req.pack()}, action="health_ask_what", service="health services", options=options)
        return await self.start(gw, tool_id, req)

    async def start(self, gw, tool_id: str, req: Request) -> Outcome:
        gw.st["domain"] = gw.call.service_domain = DOMAIN
        gw.raw_json = {"health": tool_id, "request": req.pack()}
        if not self.providers:
            return gw.reply("partner_unavailable", action="health_unavailable", service="health services")
        handlers = {"health.find_facility": self.find, "health.facility_status": self.status, "health.available_slots": self.slots, "health.book_appointment": self.book,
                    "health.cancel_appointment": self.cancel, "health.drug_availability": self.drug, "health.ask_the_centre": self.ask_centre}
        return await handlers[tool_id](gw, req)

    def ask(self, gw, step: str, tool_id: str, req: Request, question: str, **state) -> Outcome:
        return gw.ask("partner_say", {"kind": PENDING, "domain": DOMAIN, "step": step, "tool": tool_id, "req": req.pack(), "tries": req.tries, **state},
                      action=f"health_ask_{step}", answer=question)

    def ask_where(self, gw, tool_id: str, req: Request, question: str | None = None) -> Outcome:
        """The place, area or medicine is still missing. Asked once more at most: after that the caller is told how to try again."""
        if req.tries >= MAX_TRIES:
            return self.say(gw, GIVE_UP.get(tool_id, GIVE_UP_PLACE), "health_where_dropped")
        return self.ask(gw, "where", tool_id, req, question or QUESTIONS[tool_id])

    def say(self, gw, text: str, action: str) -> Outcome:
        gw.st["pending"] = None
        return gw.reply("partner_answer", action=action, answer=text)

    def nothing_reachable(self, gw) -> Outcome:
        return gw.reply("partner_unavailable", action="health_unreachable", service="the clinics' systems")

    # ---- find ------------------------------------------------------------------------------------------

    async def find(self, gw, req: Request) -> Outcome:
        emergency = req.need == "emergency care" or req.red_flag
        if emergency:
            req.need = "emergency care"
        if not req.area:
            if emergency:
                return self.ask_where(gw, "health.find_facility", req, f"If this is an emergency, call {gazetteer.EMERGENCY_NUMBER} now. Which part of Lagos are you in, so I can find the nearest emergency care?")
            return self.ask_where(gw, "health.find_facility", req)
        found, reachable = await self.places(gw, req, kind="pharmacies" if req.need == "pharmacy" else "clinics", limit=3)
        if not reachable:
            return self.nothing_reachable(gw)
        if not found:
            return self.say(gw, "I could not find a place offering that near you.", "health_find_none")
        now = self.now()
        lead = f"If this is an emergency, call {gazetteer.EMERGENCY_NUMBER} now. " if emergency else ""
        what = "emergency care" if emergency else "pharmacy" if req.need == "pharmacy" else f"place for {req.need}" if req.need else "place"
        text = f"{lead}The nearest {what} is {speak.place_line(found[0], now, queue=not emergency, fee=not emergency)}"
        if found[1:]:
            text += " Others are " + "; ".join(speak.option_line(f, brief=emergency) for f in found[1:3]) + "."
        bookable = [f for f in found if f["bookable"]]
        if emergency or not bookable:
            return self.say(gw, text, "health_find")
        return self.ask(gw, "offer_book", "health.find_facility", req, f"{text} Would you like me to book an appointment at one of these?", options=self.compact(bookable))

    @staticmethod
    def compact(found: list[dict]) -> list[dict]:
        """What is kept between turns about each option: enough to name it, say it again and ask its provider about it."""
        return [{"d": f["d"], "id": f["id"], "n": f["name"], "sn": f.get("sms_name") or f["name"], "line": speak.option_line(f), "fee": f.get("fee", 0), "w": f.get("wait_minutes", 0),
                 "km": f.get("km"), "k": speak.kind_phrase(f), "services": f.get("services", [])} for f in found]

    @staticmethod
    def options_question(options: list[dict], area: str | None, lead: str = "") -> str:
        spoken = " ".join(f"The {ORDINAL_WORDS[i]} is {o['line']}." for i, o in enumerate(options))
        return f"{lead}I found {len(options)} places{f' near {area}' if area else ''}. {spoken} Which one would you like?"

    # ---- which place ---------------------------------------------------------------------------------

    @staticmethod
    def names_a_place(text: str | None, area: str) -> bool:
        """'Surulere' names an area; 'Aguda' or 'Surulere Care' names a place."""
        left = [t for t in tokens(text or "") if t not in tokens(area) and t not in PLACE_WORDS and t not in ("in", "at", "the", "near", "around", "a", "an", "my", "i", "am")]
        return bool(left)

    async def resolve_place(self, gw, req: Request, tool_id: str, *, bookable: bool = False) -> tuple[dict | None, Outcome | None]:
        """The one place the caller means, or the question that settles it. Returns (place, None) or (None, the outcome to give)."""
        text = req.facility or ""
        area = gazetteer.match_area(text) if text else None
        if area and not req.area:
            req.area = area.name
        query = text if text and not (area and not self.names_a_place(text, area.name)) else None
        found, reachable = await self.places(gw, Request(area=req.area), kind=None if tool_id != "health.book_appointment" else "clinics", bookable=bookable, query=query, limit=MAX_OPTIONS)
        if not reachable:
            return None, self.nothing_reachable(gw)
        if not found:
            req.tries += 1
            return None, self.ask_where(gw, tool_id, req, "I could not find that. Which clinic, or which part of Lagos?")
        options = self.compact(found[:MAX_OPTIONS])
        if len(options) == 1:
            return options[0], None
        return None, self.ask(gw, "choose", tool_id, req, self.options_question(options, req.area if not query else None), options=options)

    async def status(self, gw, req: Request) -> Outcome:
        if not (req.facility or req.area):
            return self.ask_where(gw, "health.facility_status", req)
        req.facility = req.facility or req.area
        place, outcome = await self.resolve_place(gw, req, "health.facility_status")
        return outcome or await self.status_of(gw, req, place)

    async def status_of(self, gw, req: Request, place: dict) -> Outcome:
        full = await self.fetch_place(gw, req, place)
        if not full:
            return self.nothing_reachable(gw)
        offers = join_names(full.get("services", [])[:4], "and")
        return self.say(gw, speak.place_line(full, self.now()) + (f" It offers {offers}." if offers else ""), "health_status")

    async def slots(self, gw, req: Request) -> Outcome:
        if not (req.facility or req.area):
            return self.ask_where(gw, "health.available_slots", req)
        req.facility = req.facility or req.area
        place, outcome = await self.resolve_place(gw, req, "health.available_slots", bookable=True)
        return outcome or await self.slots_of(gw, place)

    async def slots_of(self, gw, place: dict) -> Outcome:
        answer = await self.provider_call(gw, self.spec_of(place), "slots", {"facility_id": place["id"], "count": 3})
        if answer is None:
            return self.nothing_reachable(gw)
        now = self.now()
        found = [datetime.fromisoformat(s) for s in answer.get("slots", [])]
        if not found:
            return self.say(gw, f"{place['n']} has no free times in the next two weeks.", "health_slots_none")
        times = join_names([f"{say_day(s.date(), now.date())} at {say_time(s)}" for s in found], "and")
        return self.say(gw, f"The next free times at {place['n']} are {times}.", "health_slots")

    # ---- booking -------------------------------------------------------------------------------------------------

    async def book(self, gw, req: Request) -> Outcome:
        if not (req.facility or req.area):
            return self.ask_where(gw, "health.book_appointment", req)
        need = req.need if req.need not in (None, "pharmacy", "emergency care") else None
        text = req.facility or ""
        area = gazetteer.match_area(text) if text else None
        if area and not req.area:
            req.area = area.name
        query = text if text and not (area and not self.names_a_place(text, area.name)) else None
        found, reachable = await self.places(gw, Request(need=need, area=req.area), kind="clinics", bookable=True, query=query, limit=MAX_OPTIONS)
        if not reachable:
            return self.nothing_reachable(gw)
        if not found:
            req.tries += 1
            return self.ask_where(gw, "health.book_appointment", req, "I could not find a clinic like that. Which clinic, or which part of Lagos?")
        options = self.compact(found[:MAX_OPTIONS])
        if len(options) == 1:
            return await self.book_at(gw, req, options[0])
        return self.ask(gw, "choose", "health.book_appointment", req, self.options_question(options, req.area if not query else None), options=options)

    async def book_at(self, gw, req: Request, place: dict) -> Outcome:
        """The place is settled. Ask for the day and time if they were not said, then offer the first real slot that fits."""
        found = parse_when(req.when or "", self.now())
        if said_nothing_about_time(found):
            return self.ask(gw, "when", "health.book_appointment", req, f"What day and time would you like at {place['n']}? For example tomorrow morning.", place=place)
        return await self.offer_slot(gw, req, place, found)

    async def offer_slot(self, gw, req: Request, place: dict, found: dict, note: str = "") -> Outcome:
        spec, now = self.spec_of(place), self.now()
        args = {"facility_id": place["id"], "count": 1, **({"day": found["day"].isoformat()} if found["day"] else {}),
                **({"window": list(found["window"])} if found["window"] else {}), **({"at": found["at"]} if found["at"] is not None else {})}
        answer = await self.provider_call(gw, spec, "slots", args)
        if answer is None:
            return self.nothing_reachable(gw)
        slots = answer.get("slots", [])
        if not slots and found["day"]:   # nothing that day: the next free time after it
            answer = await self.provider_call(gw, spec, "slots", {"facility_id": place["id"], "count": 1})
            slots = (answer or {}).get("slots", [])
            note = note or f"There is nothing free {say_day(found['day'], now.date())} at {place['n']}. "
        if not slots:
            return self.say(gw, f"{place['n']} has no free times in the next two weeks.", "health_book_none")
        start = datetime.fromisoformat(slots[0])
        return self.ask(gw, "confirm", "health.book_appointment", req,
                        f"{note}I can book you at {place['n']} {say_day(start.date(), now.date())} at {say_time(start)}. Shall I book it?", place=place, slot=slots[0])

    async def make_booking(self, gw, req: Request, place: dict, slot: str) -> Outcome:
        visit = req.need if req.need in place.get("services", []) else "general consultation"
        answer = await self.provider_call(gw, self.spec_of(place), "book", {"facility_id": place["id"], "start": slot, "visit": visit})
        if answer is None:
            return self.nothing_reachable(gw)
        now = self.now()
        if not answer.get("ok"):   # taken a moment ago, or no longer offered: offer the next one that fits what was asked
            return await self.offer_slot(gw, req, place, parse_when(req.when or "", now), note="That time was just taken. ")
        appt = answer["appointment"]
        start, code = datetime.fromisoformat(appt["starts"]), appt["code"]
        said = (f"Done. You are booked at {place['n']} {say_day(start.date(), now.date())} at {say_time(start)}. Your booking code is {' '.join(code.split('-')[1])}. "
                "Please arrive 15 minutes early.")
        sms_name = place.get("sn") or place["n"]
        text = f"Booked: {sms_name}, {start.strftime('%a')} {start.day} {start.strftime('%b')} at {start.strftime('%H:%M')}. Code {code}. Please arrive 15 minutes early."
        self.remember_visit(gw, place, appt)
        if await self.text_caller(gw, text):
            said += " I have also texted you the details."
        return self.say(gw, said, "health_booked")

    def remember_visit(self, gw, place: dict, appt: dict) -> None:
        """Talk keeps the code, the place and the time of a visit it booked, so it can remind the patient and follow up. Nothing about why they are going."""
        from ...models import HealthAppointment

        gw.db.add(HealthAppointment(customer_id=gw.customer.id, provider=place["d"], code=appt["code"], facility_name=place["n"], sms_name=place.get("sn") or place["n"],
                                    starts_local=appt["starts"]))

    async def text_caller(self, gw, body: str) -> bool:
        """Only ever to the caller's own number. True when the text was accepted, so the call says it was sent only when it was."""
        from ...services.notify import send_sms

        note = await send_sms(gw.db, gw.svc, to=gw.customer.phone, body=body[:300], template="health_notice", customer_id=gw.customer.id)
        return note.status in ("sent", "mock_sent")

    # ---- cancelling --------------------------------------------------------------------------------------------------

    async def cancel(self, gw, req: Request) -> Outcome:
        answered = await self.ask_all(gw, "upcoming", {}, "clinics")
        if not answered and self.specs("clinics"):
            return self.nothing_reachable(gw)
        mine = sorted(({**a, "d": spec.domain} for spec, answer in answered for a in answer.get("appointments", [])), key=lambda a: a["starts"])
        if not mine:
            return self.say(gw, "I do not see an appointment booked for you.", "health_cancel_none")
        first, now = mine[0], self.now()
        start = datetime.fromisoformat(first["starts"])
        return self.ask(gw, "cancel_confirm", "health.cancel_appointment", req,
                        f"Your next appointment is at {first['facility_name']} {say_day(start.date(), now.date())} at {say_time(start)}. Shall I cancel it?",
                        appointment={"d": first["d"], "code": first["code"]})

    # ---- the centre's patient guide ----------------------------------------------------------------------------------------

    async def ask_centre(self, gw, req: Request) -> Outcome:
        """A basic question, answered only from the centre's own patient guide. What the guide does not cover is not guessed at: it goes to the care team."""
        question = (req.utterance or "").strip()
        answered = await self.ask_all(gw, "knowledge", {"query": question, "limit": 3}, "clinics")
        if not answered:
            return self.nothing_reachable(gw)
        entries = sorted((e for _, a in answered for e in a.get("entries", [])), key=lambda e: -e["score"])[:3]
        gw.raw_json = {"health": "ask_the_centre", "entries": [e["id"] for e in entries]}
        if not entries:
            return await gw.give_up("health_not_in_the_guide", question)
        if entries[0]["id"] == "urgent-signs":   # a warning sign is spoken exactly as the centre wrote it, never reworded
            return self.say(gw, entries[0]["answer"], "health_urgent_signs")
        from .lookup import spoken_answer

        return await spoken_answer(gw, question, [e["answer"] for e in entries])

    # ---- medicines --------------------------------------------------------------------------------------------------------

    async def drug(self, gw, req: Request) -> Outcome:
        if not req.drug:
            return self.ask_where(gw, "health.drug_availability", req)
        args = {"drug": req.drug, **({"lat": AREAS[req.area].lat, "lon": AREAS[req.area].lon} if req.area else {})}
        answered = await self.ask_all(gw, "stock", args, "pharmacies")
        if not answered:
            return self.nothing_reachable(gw)
        drug = next((a["drug"] for _, a in answered if a.get("drug")), None)
        if not drug:
            req.tries += 1
            req.drug = None
            return self.ask_where(gw, "health.drug_availability", req, "I did not catch the medicine. Say the name as it is written on the pack.")
        shops = sorted((p for _, a in answered for p in a.get("pharmacies", [])), key=lambda p: (p["km"] is None, p["km"] if p["km"] is not None else 0, not p["open_now"], p["name"]))
        if not shops:
            return self.say(gw, f"None of the pharmacies I can see have {drug['name']} right now.", "health_drug_none")
        text = f"You can find {drug['name']} at {join_names([p['name'] for p in shops[:2]])}. A pack costs about {drug['price']:,} naira."
        if drug["prescription"]:
            text += " This one needs a prescription from a doctor."
        night = next((p for p in shops if p.get("all_day")), None)
        if night and night not in shops[:2]:
            text += f" {night['name']} is open all night."
        return self.say(gw, text, "health_drug")

    # ---- the caller's answers ------------------------------------------------------------------------------------------

    async def resolve_pending(self, gw, pending: dict, transcript: str, alternatives: list[str] | None) -> Outcome | None:
        st, step, tool_id = gw.st, pending["step"], pending.get("tool")
        req = Request.unpack(pending.get("req"))
        tries = pending.get("tries", 0)

        if step == "what":
            u = await gw.understand(transcript, alternatives)
            st["pending"] = None
            fresh = await self.read(gw, transcript, alternatives)
            req = Request.unpack({**req.__dict__, **{k: v for k, v in fresh.__dict__.items() if v not in (None, "", False, 0)}})
            tool = u.tool if u.tool in dict(TOOLS) else None
            return await self.start(gw, tool, req) if tool else None

        answer = answer_of(transcript)
        if step in ("offer_book", "confirm", "cancel_confirm"):
            plain = await gw.answer_to(transcript, alternatives)  # the model's reading of a yes or a no, said in any way
            answer = {"yes": "yes", "no": "no"}.get(plain, answer)
        gw.raw_json = {"health_step": step, "answer": answer}

        if step == "offer_book":
            st["pending"] = None
            if answer == "no":
                return gw.reply("declined", action="health_offer_declined")
            if answer != "yes":
                return None
            options = pending["options"]
            if len(options) == 1:
                return await self.book_at(gw, req, options[0])
            return self.ask(gw, "choose", "health.book_appointment", req, self.options_question(options, req.area), options=options)

        if step == "choose":
            options = pending["options"]
            index = await self.pick_option(gw, transcript, alternatives, options)
            if index is None:
                if tries + 1 >= MAX_TRIES:
                    st["pending"] = None
                    return gw.reply("declined", action="health_choose_dropped")
                req.tries = tries + 1
                return self.ask(gw, "choose", tool_id, req, self.options_question(options, None, lead="Sorry, which one? "), options=options)
            st["pending"] = None
            place = options[index]
            if tool_id == "health.facility_status":
                return await self.status_of(gw, req, place)
            if tool_id == "health.available_slots":
                return await self.slots_of(gw, place)
            return await self.book_at(gw, req, place)

        if step == "where":
            st["pending"] = None
            fresh = await self.read(gw, transcript, alternatives)
            area = gazetteer.match_area(transcript)
            req.tries = tries + 1
            req.need = req.need or fresh.need
            if tool_id == "health.drug_availability":
                req.drug = fresh.drug or transcript.strip()[:80]
            elif tool_id == "health.find_facility":
                req.area = (area.name if area else None) or fresh.area
            else:
                req.area = (area.name if area else None) or fresh.area or req.area
                req.facility = fresh.facility or transcript.strip()[:80]
            return await self.start(gw, tool_id, req)

        if step == "when":
            place, now = pending["place"], self.now()
            found = parse_when(transcript, now)
            if said_nothing_about_time(found):
                found = parse_when((await self.read(gw, transcript, alternatives)).when or "", now)
            if said_nothing_about_time(found):
                if tries + 1 >= MAX_TRIES:
                    st["pending"] = None
                    return gw.reply("declined", action="health_when_dropped")
                req.tries = tries + 1
                return self.ask(gw, "when", tool_id, req, "Sorry, what day and time? For example tomorrow morning or Friday at 2 pm.", place=place)
            st["pending"] = None
            req.when = transcript.strip()[:120]
            return await self.offer_slot(gw, req, place, found)

        if step == "confirm":
            place, now = pending["place"], self.now()
            fresh = parse_when(transcript, now)
            if not said_nothing_about_time(fresh) and (tokens(transcript) or [""])[0] not in YES_OPENERS:   # a different day or time, not a yes to the one offered
                st["pending"] = None
                req.when = transcript.strip()[:120]
                return await self.offer_slot(gw, req, place, fresh)
            if answer == "yes":
                st["pending"] = None
                return await self.make_booking(gw, req, place, pending["slot"])
            if answer == "no":
                if tries + 1 >= 3:
                    st["pending"] = None
                    return gw.reply("declined", action="health_confirm_dropped")
                req.tries = tries + 1
                return self.ask(gw, "when", tool_id, req, "No problem. What day and time would suit you better?", place=place)
            st["pending"] = None
            return None

        if step == "cancel_confirm":
            st["pending"] = None
            if answer == "yes":
                appt = pending["appointment"]
                done = await self.provider_call(gw, self.spec_of(appt), "cancel", {"code": appt["code"]})
                if done is None:
                    return self.nothing_reachable(gw)
                if not done.get("ok"):
                    return self.say(gw, "I could not cancel that one. Please call the clinic.", "health_cancel_failed")
                from ...models import HealthAppointment

                row = gw.db.scalar(select(HealthAppointment).where(HealthAppointment.code == appt["code"], HealthAppointment.customer_id == gw.customer.id))
                if row:
                    row.status = "cancelled"
                sent = await self.text_caller(gw, f"Cancelled: your appointment {appt['code']}.")
                return self.say(gw, "Done. Your appointment is cancelled." + (" I have also texted you to confirm." if sent else ""), "health_cancelled")
            if answer == "no":
                return self.say(gw, "Okay. I have kept your appointment.", "health_cancel_kept")
            return None

        st["pending"] = None
        return None

    async def pick_option(self, gw, text: str, alternatives: list[str] | None, options: list[dict]) -> int | None:
        """Which of the offered places the caller means: 'the first', a name, 'the private one', 'the cheaper one'. The model reads it only when these do not."""
        said = tokens(text)
        for w in said:
            if w in ORDINALS and ORDINALS[w] < len(options):
                return ORDINALS[w]
        named = [i for i, o in enumerate(options) if {t for t in tokens(o["n"]) if t not in PLACE_WORDS} & set(said)]
        if len(named) == 1:
            return named[0]
        kinds = {KIND_WORDS[w] for w in said if w in KIND_WORDS}
        if kinds:
            matching = [i for i, o in enumerate(options) if all(k in o["k"] for k in kinds)]
            if matching:
                return matching[0]     # the list is nearest first; the offer that follows names the place, so a wrong guess is heard and corrected
        if any(w in said for w in ("cheaper", "cheapest", "cheap", "lowest")):
            return min(range(len(options)), key=lambda i: options[i]["fee"])
        if any(w in said for w in ("nearest", "closest", "closer")):
            return min(range(len(options)), key=lambda i: options[i]["km"] if options[i]["km"] is not None else 99)
        if any(w in said for w in ("quicker", "quickest", "faster", "fastest", "shorter", "shortest")):
            return min(range(len(options)), key=lambda i: options[i]["w"])
        try:
            schema = {"type": "object", "additionalProperties": False, "required": ["choice"],
                      "properties": {"choice": {"type": "string", "enum": [str(i + 1) for i in range(len(options))] + ["none"]}}}
            listing = "\n".join(f"{i + 1}. {o['line']}" for i, o in enumerate(options))
            pick, _raw = await gw.svc.llm.extract(PickedOption, f"CHOICE\nA caller was offered these places and answered. Say the number of the one they mean, or none.\n{listing}", text,
                                                  alternatives, json_schema=schema)
            if pick.choice and pick.choice.isdigit() and 1 <= int(pick.choice) <= len(options):
                return int(pick.choice) - 1
        except Exception as exc:
            log.warning("could not read the choice: %s", exc)
        return None
