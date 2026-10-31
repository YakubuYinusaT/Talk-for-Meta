"""One provider system, the other side of Talk's HTTP connector: a state health network, a private clinic group or a pharmacy chain.

Talk sends a signed JSON message, `list_tools` or `call`, and gets facts back as data: which of this provider's places fit, who is open, how long the queue is,
which slots are free, whether a booking really went through. The provider keeps its own records (store.py) and decides only what is true in its own system.
What to say to the caller, and which provider to ask, is Talk's job. A provider sees a pseudonymous caller reference and never a phone number.
"""

import hashlib
import hmac
import html
import json
import time
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import data, knowledge
from .store import HospitalStore
from .when import words

GENERIC = {"clinic", "hospital", "health", "centre", "center", "general", "pharmacy", "medical", "community", "the", "at", "in", "a", "to", "of", "and", "my", "i", "want", "book",
           "an", "appointment", "for", "near", "around", "please", "go", "see", "doctor", "visit", "everwell", "private", "state"}

TOOLS = [
    {"name": "search", "description": "Find this provider's places near a point, by need or by name, with hours, queue and fee",
     "params": [{"name": "lat", "prompt": "Latitude", "required": False}, {"name": "lon", "prompt": "Longitude", "required": False},
                {"name": "need", "prompt": "The service needed", "required": False}, {"name": "query", "prompt": "A place's name", "required": False}]},
    {"name": "slots", "description": "Free appointment times at one of this provider's places",
     "params": [{"name": "facility_id", "prompt": "Which place?", "required": True}]},
    {"name": "book", "description": "Book an appointment at one of this provider's places", "risk": "service",
     "params": [{"name": "facility_id", "prompt": "Which place?", "required": True}, {"name": "start", "prompt": "When?", "required": True}]},
    {"name": "cancel", "description": "Cancel the caller's own appointment", "risk": "service", "params": [{"name": "code", "prompt": "The booking code", "required": True}]},
    {"name": "upcoming", "description": "The caller's own coming appointments with this provider", "params": []},
    {"name": "stock", "description": "Which of this provider's pharmacies have a medicine in stock",
     "params": [{"name": "drug", "prompt": "Which medicine?", "required": True}]},
    {"name": "knowledge", "description": "Entries from this provider's own patient guide that answer a question",
     "params": [{"name": "query", "prompt": "The question", "required": True}]},
    {"name": "visit_status", "description": "What became of one of the caller's own visits: still booked, attended, missed or cancelled",
     "params": [{"name": "code", "prompt": "The booking code", "required": True}]},
]


def sign(secret: str, stamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def ok(**payload) -> dict:
    return {"status": "ok", "say": "", "data": payload}


def refuse(reason: str) -> dict:
    """The provider understood and said no. The reason doubles as `say` so Talk treats it as an answer, not as a broken connection."""
    return {"status": "error", "say": reason, "data": {"reason": reason}}


def name_scores(text: str, pool: list[data.Facility]) -> list[tuple[float, data.Facility]]:
    """The places a caller named, best match first, with how well each matched. Words like clinic or hospital say nothing about which one, so they are ignored, and a word from a place's own
    name counts for more than the area it is in ("Aguda" is an area nickname, but it is also the name of one health centre)."""
    tokens = [t for t in words(text) if t not in GENERIC]
    scored: list[tuple[float, data.Facility]] = []
    for f in pool:
        names = {w for w in words(f.name) if w not in GENERIC}
        places = set(words(f.area)) | {w for a in data.AREAS[f.area].aliases for w in words(a)}
        score = 0.0
        for token in tokens:
            if token in names:
                score += 1.0
            elif token in places:
                score += 0.4
            elif len(token) > 4 and any(SequenceMatcher(None, token, k).ratio() >= 0.85 for k in names if len(k) > 4):
                score += 0.8
        if score:
            scored.append((score, f))
    scored.sort(key=lambda pair: -pair[0])
    best = scored[0][0] if scored else 0.0
    return [(score, f) for score, f in scored if score >= max(best - 0.01, 0.8)]      # naming a place needs a word of its own name, not only its area


def name_matches(text: str, pool: list[data.Facility]) -> list[data.Facility]:
    return [f for _, f in name_scores(text, pool)]


class Provider:
    def __init__(self, network: data.Network, store: HospitalStore):
        self.network, self.store = network, store

    def describe(self, f: data.Facility, now: datetime, km: float | None) -> dict:
        span = self.store.hours_today(f, now)
        opens_next = None
        if not self.store.is_open(f, now) and not f.hours.get(0) == (0, 24):
            for n in range(0, 8):
                day = now.date() + timedelta(days=n)
                hours = f.hours.get(day.weekday())
                if hours and not (n == 0 and now.hour >= hours[1]):
                    opens_next = {"day": day.isoformat(), "hour": hours[0]}
                    break
        people, wait = self.store.queue(f, now)
        return {"id": f.id, "name": f.name, "kind": f.kind, "area": f.area, "address": f.address, "km": None if km is None else round(km, 1), "services": list(f.services),
                "open_now": self.store.is_open(f, now), "all_day": f.hours.get(0) == (0, 24), "closes": span[1] if span and span[0] <= now.hour < span[1] else None,
                "opens_next": opens_next, "emergency": f.emergency, "bookable": f.booking, "fee": f.fee, "waiting": people, "wait_minutes": wait,
                "owner": self.network.owner, "provider": self.network.name, "sms_name": f.sms_name or f.name}

    # ---- the operations ----------------------------------------------------------------------------

    def call(self, tool: str, args: dict, ctx: dict) -> dict:
        handler = {"search": self.search, "slots": self.slots, "book": self.book, "cancel": self.cancel, "upcoming": self.upcoming, "stock": self.stock,
                   "knowledge": self.knowledge, "visit_status": self.visit_status}.get(tool)
        if not handler:
            return refuse("unknown_tool")
        try:
            return handler(args, ctx)
        except (KeyError, ValueError, TypeError):
            return refuse("bad_request")

    def search(self, args: dict, ctx: dict) -> dict:
        now = self.store.clock()
        pool = list(self.store.facilities)
        need, query = str(args.get("need") or ""), str(args.get("query") or "")
        if need == "pharmacy":
            pool = [f for f in pool if f.kind == "pharmacy"]
        elif need:
            pool = [f for f in pool if need in f.services or (need == "emergency care" and f.emergency)]
        if args.get("bookable"):
            pool = [f for f in pool if f.booking]
        if args.get("ids"):
            pool = [f for f in pool if f.id in set(args["ids"])]
        elif not need:
            pool = [f for f in pool if not f.specialist or (query and name_matches(query, [f]))]   # a specialist centre is not a general clinic
        matches: dict[str, float] = {}
        if query:
            scored = name_scores(query, pool)
            matches = {f.id: score for score, f in scored}
            pool = [f for _, f in scored]
        lat, lon = args.get("lat"), args.get("lon")
        if lat is not None and lon is not None:
            ranked = [(f, km) for f, km in self.store.nearest(float(lat), float(lon), pool)]
        else:
            ranked = [(f, None) for f in sorted(pool, key=lambda f: f.name)]
        limit = max(1, min(int(args.get("limit") or 5), 10))
        return ok(facilities=[{**self.describe(f, now, km), **({"match": matches[f.id]} if f.id in matches else {})} for f, km in ranked[:limit]])

    def slots(self, args: dict, ctx: dict) -> dict:
        f = self.store.by_id.get(str(args.get("facility_id") or ""))
        if not f:
            return refuse("unknown_facility")
        now = self.store.clock()
        window = tuple(args["window"]) if args.get("window") else None
        day = date.fromisoformat(args["day"]) if args.get("day") else None
        at = args.get("at")
        found = self.store.next_slots(f, now, max(1, min(int(args.get("count") or 1), 5)), window=window, at=None if at is None else int(at), day=day)
        return ok(facility_id=f.id, slots=[s.isoformat() for s in found], bookable=f.booking)

    def book(self, args: dict, ctx: dict) -> dict:
        caller = str(ctx.get("caller_id") or "")
        f = self.store.by_id.get(str(args.get("facility_id") or ""))
        if not caller:
            return refuse("unknown_caller")
        if not f or not f.booking:
            return refuse("unknown_facility")
        start = datetime.fromisoformat(str(args.get("start")))
        if start < self.store.clock() or (start - self.store.clock()).days > 14:
            return refuse("slot_not_offered")
        visit = str(args.get("visit") or data.GENERAL)
        made = self.store.book(f, start, visit if visit in f.services else data.GENERAL, caller)
        if not made:
            return refuse("taken")
        return ok(appointment={"code": made["code"], "facility_id": f.id, "facility_name": f.name, "starts": start.isoformat(), "visit": made["visit"]})

    def cancel(self, args: dict, ctx: dict) -> dict:
        caller = str(ctx.get("caller_id") or "")
        return ok(cancelled=True) if caller and self.store.cancel(str(args.get("code") or ""), caller) else refuse("not_found")

    def upcoming(self, args: dict, ctx: dict) -> dict:
        caller = str(ctx.get("caller_id") or "")
        found = self.store.upcoming(caller, self.store.clock()) if caller else []
        return ok(appointments=[{"code": a["code"], "facility_id": a["facility_id"], "facility_name": self.store.by_id[a["facility_id"]].name,
                                 "starts": a["starts"].isoformat(), "visit": a["visit"]} for a in found])

    def knowledge(self, args: dict, ctx: dict) -> dict:
        """Entries from this provider's patient guide, if it has one. Talk answers only from what comes back; an empty list means the guide does not cover it."""
        if self.network.slug != "onc-centre":
            return ok(entries=[])
        return ok(entries=knowledge.search(str(args.get("query") or ""), max(1, min(int(args.get("limit") or 3), 5))), provider=self.network.name)

    def visit_status(self, args: dict, ctx: dict) -> dict:
        caller = str(ctx.get("caller_id") or "")
        found = self.store.visit(str(args.get("code") or ""), caller) if caller else None
        if not found:
            return refuse("not_found")
        return ok(code=found["code"], status=found["status"], starts=found["starts"].isoformat(), facility_name=self.store.by_id[found["facility_id"]].name, visit=found["visit"])

    def stock(self, args: dict, ctx: dict) -> dict:
        now = self.store.clock()
        drug = self.store.find_drug(str(args.get("drug") or ""))
        if not drug:
            return ok(drug=None, pharmacies=[])
        lat, lon = args.get("lat"), args.get("lon")
        pharmacies = self.store.pharmacies_with(drug)
        ranked = self.store.nearest(float(lat), float(lon), pharmacies) if lat is not None and lon is not None else [(f, None) for f in pharmacies]
        out = []
        for f, km in ranked[:6]:
            row = self.describe(f, now, km)
            row["qty"], row["price"] = data.stock_level(f.id, drug.id), drug.price
            out.append(row)
        return ok(drug={"id": drug.id, "name": drug.name, "price": drug.price, "prescription": drug.prescription}, pharmacies=out)


# ---- the web service ------------------------------------------------------------------------------------------


def create_provider_app(network: data.Network, secret: str, store: HospitalStore | None = None, desk_key: str = "") -> FastAPI:
    """`desk_key` lets the front desk mark visits as attended or missed from its page (open the page with ?key=...). Empty: the page only shows the book."""
    store = store or HospitalStore(facilities=network.facilities)
    provider = Provider(network, store)
    app = FastAPI(title=network.name, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.provider = store, provider

    def verified(request: Request, body: bytes) -> None:
        stamp = request.headers.get("x-sofa-timestamp", "")
        good = stamp.isdigit() and abs(time.time() - int(stamp)) <= 300  # an old message cannot be sent again later
        if not (good and hmac.compare_digest(sign(secret, stamp, body), request.headers.get("x-sofa-signature", ""))):
            raise HTTPException(status_code=401, detail="bad signature")

    @app.post("/rpc")
    async def rpc(request: Request):
        body = await request.body()
        verified(request, body)
        try:
            message = json.loads(body)
        except ValueError:
            raise HTTPException(status_code=400, detail="not JSON") from None
        if message.get("op") == "list_tools":
            return {"tools": TOOLS}
        if message.get("op") == "call":
            return provider.call(str(message.get("tool") or ""), message.get("arguments") or {}, message.get("context") or {})
        raise HTTPException(status_code=400, detail="unknown operation")

    @app.post("/desk/mark")
    async def mark(request: Request):
        form = await request.form()
        if not desk_key or not hmac.compare_digest(str(form.get("key", "")), desk_key):
            raise HTTPException(status_code=403, detail="the desk key is needed")
        store.mark(str(form.get("code", "")), str(form.get("outcome", "")))
        return RedirectResponse(f"../?key={form.get('key')}", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def desk(key: str = ""):
        """This provider's front desk: its places with the queue now, and the appointments booked. Made-up data, labelled as such."""
        now = store.clock()
        rows = []
        for f in store.facilities:
            people, wait = store.queue(f, now)
            state = "open" if store.is_open(f, now) else "closed"
            rows.append(f"<tr><td>{html.escape(f.name)}</td><td>{html.escape(f.kind)}</td><td>{html.escape(f.area)}</td><td>{state}</td><td>{people}</td><td>{wait} min</td>"
                        f"<td>{f.fee:,}</td></tr>")
        booked = []
        marking = bool(desk_key) and hmac.compare_digest(key, desk_key)
        for r in store.booked_on(now):
            f = store.by_id[r["facility_id"]]
            when = datetime.fromisoformat(r["starts"])
            buttons = ""
            if marking and r["status"] == "booked":
                buttons = "".join(f"<form method=post action=desk/mark style=display:inline><input type=hidden name=key value='{html.escape(key)}'>"
                                  f"<input type=hidden name=code value='{html.escape(r['code'])}'><button name=outcome value={o}>{label}</button></form> "
                                  for o, label in (("attended", "Attended"), ("no_show", "Did not come")))
            booked.append(f"<tr><td>{html.escape(r['code'])}</td><td>{html.escape(f.name)}</td><td>{when:%a %d %b %H:%M}</td><td>{html.escape(r['visit'])}</td>"
                          f"<td>{html.escape(r['caller_ref'][:8])}</td><td>{html.escape(r['status'].replace('_', ' '))}</td><td>{buttons}</td></tr>")
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(network.name)}</title>"
            "<style>body{font:15px system-ui;margin:24px;max-width:900px}table{border-collapse:collapse;width:100%;margin:12px 0}td,th{border-bottom:1px solid #ccc;padding:6px;text-align:left}"
            ".tag{background:#fde68a;padding:4px 8px;border-radius:4px}</style>"
            f"<h1>{html.escape(network.name)}: front desk</h1><p><span class=tag>Synthetic data</span> Every place, queue, price and booking here is made up for a demonstration.</p>"
            f"<h2>Queues now ({now:%a %d %b %H:%M})</h2><table><tr><th>Place<th>Kind<th>Area<th>Now<th>Waiting<th>Estimated wait<th>Fee (naira)</tr>{''.join(rows)}</table>"
            f"<h2>Appointment book</h2><table><tr><th>Code<th>Place<th>When<th>Visit<th>Caller ref<th>Status<th></tr>{''.join(booked) or '<tr><td colspan=7>None yet</td></tr>'}</table>"
            + ("<p><span class=tag>Sample content</span> The patient guide this centre answers questions from is sample text written for the demonstration.</p>" if network.slug == "onc-centre" else "")
            +
            "<p>Callers appear only as a short reference: this system never sees a phone number.</p>")

    return app
