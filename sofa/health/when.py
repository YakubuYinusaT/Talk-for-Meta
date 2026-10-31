"""Days and times, the way people say them on a phone: reading them (tomorrow, 9:30 in the morning) and saying them back.

Times are Lagos time (UTC+1, no daylight saving) as plain datetimes with no zone, because that is the only clock a caller and a clinic share.
"""

import re
from datetime import date, datetime, timedelta, timezone

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WINDOWS = {"morning": (7 * 60, 12 * 60), "afternoon": (12 * 60, 16 * 60), "evening": (16 * 60, 20 * 60)}
YES = {"yes", "yeah", "yep", "yup", "ok", "okay", "sure", "correct", "right", "please", "alright", "fine", "confirm", "confirmed", "go", "book", "do"}
NO = {"no", "nope", "nah", "not", "don't", "dont", "cancel", "change", "another", "different", "wrong", "stop"}


def lagos_now() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None, microsecond=0)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def answer_of(text: str) -> str | None:
    """'yes', 'no', or None when the words are neither: a clear no wins over a yes ("no, please change it")."""
    found = set(words(text))
    if found & NO:
        return "no"
    if found & YES:
        return "yes"
    return None


def parse_when(text: str, now: datetime) -> dict:
    """What a caller asked for: {"day": date | None, "window": (from_minute, to_minute) | None, "at": minute | None}. Anything not said stays None."""
    t = " ".join(re.findall(r"[a-z0-9:.']+", (text or "").lower())).replace(". ", " ")  # keep the colon: 9:30 is one time
    out: dict = {"day": None, "window": None, "at": None}
    today = now.date()
    if "day after tomorrow" in t:
        out["day"] = today + timedelta(days=2)
    elif "tomorrow" in t:
        out["day"] = today + timedelta(days=1)
    elif "today" in t or "tonight" in t or "this evening" in t or "this afternoon" in t or "this morning" in t:
        out["day"] = today
    elif "next week" in t:
        out["day"] = today + timedelta(days=7 - today.weekday())  # the Monday of next week
    else:
        for index, name in enumerate(WEEKDAYS):
            if re.search(rf"\b{name}\b|\b{name[:3]}\b", t):
                out["day"] = today + timedelta(days=(index - today.weekday()) % 7 or 7)  # the next one, never today
                break
    for name, window in WINDOWS.items():
        if name in t or (name == "evening" and "tonight" in t):
            out["window"] = window
    match = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a m|p m|o'clock|oclock)?\b", t)
    if match and (match.group(3) or match.group(2) or re.search(r"\b(at|by|around)\s+\d", t)):
        hour, minute, mark = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").replace(" ", "")
        if 0 <= hour <= 23 and minute < 60:
            if mark == "pm" and hour < 12:
                hour += 12
            elif mark == "am" and hour == 12:
                hour = 0
            elif not mark and 1 <= hour <= 6:
                hour += 12  # a clinic is not open at 3 in the morning: "at 3" means the afternoon
            out["at"] = hour * 60 + minute
    return out


def said_nothing_about_time(found: dict) -> bool:
    return not (found["day"] or found["window"] or found["at"] is not None)


def say_day(day: date, today: date) -> str:
    gap = (day - today).days
    if gap == 0:
        return "today"
    if gap == 1:
        return "tomorrow"
    return f"{WEEKDAYS[day.weekday()].capitalize()} {day.day} {day.strftime('%B')}"


def say_time(moment: datetime) -> str:
    hour, minute = moment.hour, moment.minute
    if hour == 12 and minute == 0:
        return "12 noon"
    part = "in the morning" if hour < 12 else "in the afternoon" if hour < 16 else "in the evening"
    clock = hour % 12 or 12
    return f"{clock}:{minute:02d} {part}" if minute else f"{clock} {part}"


def say_hour(hour: int) -> str:
    if hour in (0, 24):
        return "midnight"
    return say_time(datetime(2000, 1, 1, hour, 0))


def join_names(names: list[str], word: str = "or") -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" {word} " + names[-1]
