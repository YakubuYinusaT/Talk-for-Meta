"""How Talk says what the providers found. Providers send facts; the wording, and what to leave out, is Talk's."""

from datetime import datetime

from .when import join_names, say_day, say_hour


def article(phrase: str) -> str:
    return f"{'an' if phrase[0] in 'aeiou' else 'a'} {phrase}"


def kind_phrase(f: dict) -> str:
    """'a state primary health centre', 'a private clinic', 'a pharmacy'."""
    if f["kind"] == "pharmacy":
        return "pharmacy"
    return f"{f['owner']} {f['kind']}"


def km_phrase(km: float | None) -> str:
    if km is None:
        return ""
    if km < 1:
        return "less than a kilometre away"
    n = round(km)
    return f"about {n} kilometre{'s' if n != 1 else ''} away"


def hours_phrase(f: dict, now: datetime) -> str:
    if f.get("all_day"):
        return "It is open day and night."
    night = " Its emergency department is open day and night." if f.get("emergency") else ""
    if f.get("open_now") and f.get("closes") is not None:
        return f"It is open until {say_hour(f['closes'])}.{night}"
    nxt = f.get("opens_next")
    if nxt:
        day = datetime.fromisoformat(nxt["day"]).date()
        when = "today" if day == now.date() else say_day(day, now.date())
        return f"It is closed now and opens {when} at {say_hour(nxt['hour'])}.{night}"
    return f"It is closed now.{night}"


def queue_phrase(f: dict) -> str:
    if not f.get("open_now"):
        return ""
    people, wait = f.get("waiting", 0), f.get("wait_minutes", 0)
    if people == 0:
        return "Nobody is waiting."
    return f"About {people} {'person is' if people == 1 else 'people are'} waiting, so expect around {wait} minutes."


def fee_phrase(f: dict) -> str:
    return f"A consultation costs about {f['fee']:,} naira." if f.get("fee") else ""


def place_line(f: dict, now: datetime, queue: bool = True, fee: bool = True) -> str:
    """One place, in full: what it is, where, how far, whether it is open, the queue and the fee."""
    parts = [f"{f['name']}, {article(kind_phrase(f))} on {f['address']}" + (f", {km_phrase(f['km'])}" if f.get("km") is not None else "") + ".",
             hours_phrase(f, now), queue_phrase(f) if queue else "", fee_phrase(f) if fee else ""]
    return " ".join(p for p in parts if p)


def option_line(f: dict, brief: bool = False) -> str:
    """One place in a list the caller chooses from: short, with the things that decide between them (an emergency list has only the kind and the distance)."""
    bits = [kind_phrase(f)]
    if f.get("km") is not None:
        bits.append(km_phrase(f["km"]))
    if brief:
        return f"{f['name']}, {join_names(bits, 'and')}"
    if f.get("open_now") and f.get("wait_minutes"):
        bits.append(f"about {f['wait_minutes']} minutes of waiting")
    elif not f.get("open_now") and not f.get("all_day"):
        bits.append("closed now")
    if f.get("fee"):
        bits.append(f"{f['fee']:,} naira for a consultation")
    return f"{f['name']}, {join_names(bits, 'and')}"
