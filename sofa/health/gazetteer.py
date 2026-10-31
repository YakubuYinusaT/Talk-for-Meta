"""What Talk itself knows about Lagos and about health needs, so a caller's words become a place and a need before any provider is asked.

Providers do not parse speech. Talk turns "I'm near the market in Ikorodu" into a point on the map and "my baby needs jabs" into a service, and passes
those on; each provider answers from its own records.
"""

import re
from difflib import SequenceMatcher

from . import data
from .when import words

EMERGENCY_NUMBER = "112"
RED_FLAGS = ("not breathing", "cannot breathe", "can't breathe", "cant breathe", "unconscious", "collapsed", "heavy bleeding", "bleeding heavily", "bleeding a lot",
             "seizure", "convulsion", "chest pain", "poison", "accident", "stroke", "snake", "burnt badly", "choking")
# a closed list of what a caller may need: the model picks one of these by meaning, the keywords are the fallback when it cannot be asked
NEEDS = (
    ("emergency care", ("emergency", "urgent", "accident")),
    (data.ONCOLOGY, ("cancer", "chemo", "chemotherapy", "radiotherapy", "oncology", "oncologist", "tumour", "tumor")),
    ("pharmacy", ("pharmacy", "chemist", "drug store", "buy medicine", "buy drugs")),
    (data.ANTENATAL, ("antenatal", "ante natal", "pregnant", "pregnancy", "prenatal", "maternity")),
    (data.IMMUNISATION, ("immunisation", "immunization", "vaccine", "vaccination", "baby jab", "child jab", "baby vaccine", "jabs")),
    (data.MALARIA, ("malaria",)),
    (data.FAMILY, ("family planning", "contraceptive", "contraception")),
    (data.LAB, ("lab test", "laboratory", "blood test", "scan", "test result")),
    (data.GENERAL, ("see a doctor", "consultation", "check up", "checkup", "sick", "not feeling well", "fever")),
)
NEED_CHOICES = [need for need, _ in NEEDS]


def match_area(text: str) -> data.Area | None:
    said = " ".join(words(text))
    if not said:
        return None
    for area in sorted(data.AREAS.values(), key=lambda a: -len(a.name)):  # "Lagos Island" before "Island"
        for name in (area.name.lower(), *area.aliases):
            if re.search(rf"\b{re.escape(name)}\b", said):
                return area
    best, best_score = None, 0.0
    for token in said.split():
        if len(token) < 4:
            continue
        for area in data.AREAS.values():
            score = SequenceMatcher(None, token, area.name.lower()).ratio() if " " not in area.name else 0.0
            if score > best_score:
                best, best_score = area, score
    return best if best_score >= 0.82 else None


def detect_need(text: str) -> str | None:
    said = " ".join(words(text))
    for need, phrases in NEEDS:
        if any(p in said for p in phrases):
            return need
    return None


def has_red_flag(text: str) -> bool:
    said = " ".join(words(text))
    return any(flag in said for flag in RED_FLAGS)
