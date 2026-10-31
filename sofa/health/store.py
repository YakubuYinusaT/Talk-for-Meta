"""The hospital system's own records: appointments and queue figures, in its own SQLite file, apart from Talk's database.

Facilities and drugs are fixed lists (data.py). What changes is kept here: who is booked when, and what the front desk says the queue is. Talk never opens
this file; it asks the hospital service over HTTP, as it would ask a real provider.
"""

import difflib
import math
import secrets
import sqlite3
import threading
import zlib
from datetime import date, datetime, timedelta

from . import data
from .when import lagos_now, words

SLOT_MINUTES = 30
BOOKING_DAYS = 14          # how far ahead a booking can be made
QUEUE_SCALE = 0.55         # the base figures are for a very busy clinic: this brings them to what a demonstration can believe
HOUR_FACTOR = {7: 0.5, 8: 0.9, 9: 1.3, 10: 1.5, 11: 1.3, 12: 1.0, 13: 0.8, 14: 0.9, 15: 0.7, 16: 0.5, 17: 0.4, 18: 0.4, 19: 0.4, 20: 0.3}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I: a code that is read aloud or typed on a keypad is not mixed up


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


class HospitalStore:
    """One provider system's records. `facilities` is what this provider runs (a state network, a private group, a pharmacy chain)."""

    def __init__(self, path: str = ":memory:", clock=lagos_now, facilities: tuple[data.Facility, ...] | None = None):
        self.clock = clock
        self.facilities = tuple(facilities if facilities is not None else data.FACILITIES)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)  # autocommit: a booking opens its own transaction
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS appointments (
                code TEXT PRIMARY KEY, facility_id TEXT NOT NULL, starts TEXT NOT NULL, visit TEXT NOT NULL,
                caller_ref TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'booked', created TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS appointments_slot ON appointments (facility_id, starts);
            CREATE INDEX IF NOT EXISTS appointments_caller ON appointments (caller_ref);
            CREATE TABLE IF NOT EXISTS queue_overrides (facility_id TEXT PRIMARY KEY, people INTEGER NOT NULL, set_at TEXT NOT NULL);
        """)
        self.by_id = {f.id: f for f in self.facilities}

    # ---- the facilities ------------------------------------------------------------------------

    def hours_today(self, f: data.Facility, now: datetime) -> tuple[int, int] | None:
        return f.hours.get(now.weekday())

    def is_open(self, f: data.Facility, now: datetime) -> bool:
        span = self.hours_today(f, now)
        return bool(span) and span[0] <= now.hour < span[1]

    def queue(self, f: data.Facility, now: datetime) -> tuple[int, int]:
        """(people waiting, minutes to wait). Made up from the time of day, with a steady wobble so two clinics never agree, unless the desk set a figure."""
        if not self.is_open(f, now):
            return 0, 0
        row = self.db.execute("SELECT people, set_at FROM queue_overrides WHERE facility_id = ?", (f.id,)).fetchone()
        if row and now - datetime.fromisoformat(row["set_at"]) < timedelta(hours=2):
            people = row["people"]
        else:
            wobble = 0.8 + (zlib.crc32(f"{f.id}|{now.date()}|{now.hour}".encode()) % 1000) / 2500  # 0.8 to 1.2
            people = round(f.base_queue * QUEUE_SCALE * HOUR_FACTOR.get(now.hour, 0.6) * wobble)
        return people, round(people * f.minutes / 5) * 5

    def set_queue(self, facility_id: str, people: int) -> None:
        """What the front desk says: the queue is this long now."""
        self.db.execute("INSERT OR REPLACE INTO queue_overrides (facility_id, people, set_at) VALUES (?, ?, ?)", (facility_id, max(0, int(people)), self.clock().isoformat()))

    def nearest(self, lat: float, lon: float, pool: list[data.Facility]) -> list[tuple[data.Facility, float]]:
        return sorted(((f, distance_km(lat, lon, f.lat, f.lon)) for f in pool), key=lambda pair: pair[1])

    # ---- appointments ----------------------------------------------------------------------------

    def free_slots(self, f: data.Facility, day: date, now: datetime) -> list[datetime]:
        span = f.hours.get(day.weekday())
        if not f.booking or not span or (day - now.date()).days > BOOKING_DAYS:
            return []
        taken = {r["starts"]: r["n"] for r in self.db.execute(
            "SELECT starts, COUNT(*) AS n FROM appointments WHERE facility_id = ? AND status = 'booked' AND starts >= ? AND starts < ? GROUP BY starts",
            (f.id, day.isoformat(), (day + timedelta(days=1)).isoformat()))}
        slots, moment = [], datetime(day.year, day.month, day.day, span[0])
        while moment + timedelta(minutes=SLOT_MINUTES) <= datetime(day.year, day.month, day.day, span[1]):
            if moment >= now + timedelta(minutes=SLOT_MINUTES) and taken.get(moment.isoformat(), 0) < f.capacity:
                slots.append(moment)
            moment += timedelta(minutes=SLOT_MINUTES)
        return slots

    def next_slots(self, f: data.Facility, now: datetime, count: int = 3, window: tuple[int, int] | None = None, at: int | None = None,
                   day: date | None = None) -> list[datetime]:
        """The first free slots that fit what was asked: on `day` only, or on the following days when no day was given."""
        found: list[datetime] = []
        days = [day] if day else [now.date() + timedelta(days=n) for n in range(0, BOOKING_DAYS + 1)]
        for d in days:
            for slot in self.free_slots(f, d, now):
                minute = slot.hour * 60 + slot.minute
                if window and not (window[0] <= minute < window[1]):
                    continue
                if at is not None and minute < at:
                    continue
                found.append(slot)
                if len(found) >= count:
                    return found
        return found

    def book(self, f: data.Facility, start: datetime, visit: str, caller_ref: str) -> dict | None:
        """Book the slot if it is still free. None when it was taken a moment ago, or the caller already holds it."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                taken = self.db.execute("SELECT COUNT(*) FROM appointments WHERE facility_id = ? AND starts = ? AND status = 'booked'", (f.id, start.isoformat())).fetchone()[0]
                mine = self.db.execute("SELECT COUNT(*) FROM appointments WHERE caller_ref = ? AND starts = ? AND status = 'booked'", (caller_ref, start.isoformat())).fetchone()[0]
                if taken >= f.capacity or mine:
                    self.db.execute("ROLLBACK")
                    return None
                code = "APT-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
                self.db.execute("INSERT INTO appointments (code, facility_id, starts, visit, caller_ref, created) VALUES (?, ?, ?, ?, ?, ?)",
                                (code, f.id, start.isoformat(), visit, caller_ref, self.clock().isoformat()))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return {"code": code, "facility_id": f.id, "starts": start, "visit": visit}

    def upcoming(self, caller_ref: str, now: datetime) -> list[dict]:
        rows = self.db.execute("SELECT * FROM appointments WHERE caller_ref = ? AND status = 'booked' AND starts >= ? ORDER BY starts", (caller_ref, now.isoformat())).fetchall()
        return [{"code": r["code"], "facility_id": r["facility_id"], "starts": datetime.fromisoformat(r["starts"]), "visit": r["visit"]} for r in rows]

    def cancel(self, code: str, caller_ref: str) -> bool:
        """Only the caller who made it can cancel it."""
        cur = self.db.execute("UPDATE appointments SET status = 'cancelled' WHERE code = ? AND caller_ref = ? AND status = 'booked'", (code, caller_ref))
        return cur.rowcount == 1

    def booked_on(self, now: datetime) -> list[sqlite3.Row]:
        """The appointment book from today on: booked, attended and missed visits (a cancelled one is gone from the book)."""
        return self.db.execute("SELECT * FROM appointments WHERE status != 'cancelled' AND starts >= ? ORDER BY starts LIMIT 200", (now.date().isoformat(),)).fetchall()

    def mark(self, code: str, outcome: str) -> bool:
        """What the front desk records when the day is done: the patient came (attended) or did not (no_show). Only a booked visit can be marked."""
        if outcome not in ("attended", "no_show"):
            return False
        return self.db.execute("UPDATE appointments SET status = ? WHERE code = ? AND status = 'booked'", (outcome, code)).rowcount == 1

    def visit(self, code: str, caller_ref: str) -> dict | None:
        """One of the caller's own visits and what became of it. Another caller's code is not found."""
        r = self.db.execute("SELECT * FROM appointments WHERE code = ? AND caller_ref = ?", (code, caller_ref)).fetchone()
        return None if not r else {"code": r["code"], "facility_id": r["facility_id"], "starts": datetime.fromisoformat(r["starts"]), "visit": r["visit"], "status": r["status"]}

    # ---- medicines ---------------------------------------------------------------------------------

    def find_drug(self, text: str) -> data.Drug | None:
        """The drug a caller named, even when it is said loosely or the recogniser spelled it a little wrong."""
        said = " ".join(words(text))
        if not said:
            return None
        best, best_score = None, 0.0
        for drug in data.DRUGS:
            for name in (drug.name.lower(), *drug.aliases):
                if f" {name} " in f" {said} ":
                    score = 1.0 + len(name) / 100  # the longest name inside the words wins
                else:
                    score = max((difflib.SequenceMatcher(None, name, token).ratio() for token in words(text) if len(token) > 3), default=0.0)
                if score > best_score:
                    best, best_score = drug, score
        return best if best_score >= 0.78 else None

    def pharmacies_with(self, drug: data.Drug) -> list[data.Facility]:
        return [f for f in self.facilities if f.kind == "pharmacy" and data.stock_level(f.id, drug.id) > 0]
