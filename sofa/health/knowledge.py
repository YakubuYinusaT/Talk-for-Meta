"""The cancer centre's patient guide, as a knowledge box a caller's questions are answered from.

The centre is building its own guide with us; until it is connected this is sample content written to show how it works, and it is labelled that way wherever it
is shown. The rule that matters is the one the real guide will follow: Talk answers only from entries in the guide, never from the model's own memory, and a
question the guide does not cover goes to the care team instead of being guessed at. Nothing here is advice about any one patient.
"""

import re
from dataclasses import dataclass

SOURCE = "Patient guide (sample content)"
STOP = {"i", "a", "an", "the", "to", "of", "my", "me", "for", "and", "is", "it", "in", "on", "do", "does", "can", "you", "what", "how", "when", "where", "who", "that", "this", "with",
        "are", "be", "at", "if", "should", "about", "i'm", "am", "your", "we", "they", "or", "so", "up", "out", "get", "will", "would", "much", "any", "please", "tell", "know",
        "centre", "center", "cancer", "ask", "question"}   # words that are in almost every question put to this guide say nothing about which entry it needs


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    keywords: str
    answer: str


ENTRIES: tuple[Entry, ...] = (
    Entry("first-visit", "What to bring to your first visit", "first visit new patient bring documents referral letter results scans card register registration",
          "Bring your referral letter, any scan or test results you have, a photo ID, and a list of the medicines you take. Please arrive 30 minutes before your appointment so you can be registered."),
    Entry("how-appointments-work", "How appointments work", "appointment time fixed queue wait book booking slot arrive on time strict",
          "At the centre every patient is given a fixed time, so you are not left in a long queue. Please arrive on time. If you cannot come, call to cancel or move your appointment so that someone else can use the slot."),
    Entry("late-or-missed", "If you are late or miss an appointment", "late missed miss forgot reschedule rebook delayed running late did not come",
          "If you are running late, call the centre as soon as you can. If you miss an appointment, call to book a new time. The team would rather hear from you than not."),
    Entry("chemo-day", "What happens on a chemotherapy day", "chemotherapy chemo day unit treatment day how long hours infusion drip what to expect",
          "Chemotherapy is given in the day unit. A visit can take several hours, so bring someone with you if you can, wear comfortable clothes, and bring a snack and water unless your team has told you otherwise."),
    Entry("what-is-chemo", "What chemotherapy is", "chemotherapy chemo what is cycles medicines cancer cells",
          "Chemotherapy is treatment with medicines that stop cancer cells from growing. It is usually given in cycles, with rest periods in between. Your oncologist will explain your own plan."),
    Entry("before-treatment", "Preparing for treatment", "prepare preparing before treatment eat eating fasting medicines instructions ready",
          "Follow the instructions your care team gave you about eating and your medicines before treatment. If you are not sure what they were, call the centre before the day."),
    Entry("urgent-signs", "When to contact the care team urgently", "urgent emergency fever temperature shivering bleeding breathing vomiting side effects warning danger call",
          "Contact the centre straight away, or come to the emergency unit, if you have a high temperature or shivering, bleeding that will not stop, trouble breathing, or vomiting that does not stop. In an emergency call 112."),
    Entry("radiotherapy", "How radiotherapy visits work", "radiotherapy radiation sessions daily weeks schedule how long",
          "Radiotherapy is usually given in short daily sessions over a number of weeks. Each session is quick. Your team will give you the schedule."),
    Entry("counselling", "Counselling and support", "counselling counseling support emotional family worried scared anxious talk group",
          "The centre offers counselling and support for patients and their families. You can ask for a counselling appointment when you book or at any visit."),
    Entry("medicines", "Collecting your medicines", "pharmacy medicines drugs collect prescription out of stock tablets",
          "After your visit, collect your prescribed medicines at the centre's pharmacy. Bring your visit paper. If a medicine is out of stock, the pharmacy will tell you where you can get it."),
    Entry("results", "Getting your results", "results scan report test biopsy result explain phone",
          "Your care team will explain your results to you at your visit. Results are not given out over the phone, so please keep your appointment."),
    Entry("fees", "Fees and payment", "fees fee cost pay payment price money afford charge billing",
          "Fees depend on the service. Ask at the billing desk when you arrive, or call the centre before your visit. The desk can also tell you what help is available."),
    Entry("contact", "How to reach the care team", "contact phone number call reach care team nurse speak hours open",
          "Call the centre on the number on your appointment card between 8 in the morning and 4 in the afternoon on weekdays. Outside those hours, in an emergency call 112."),
    Entry("bring-someone", "Bringing a family member", "family friend bring someone company accompany relative sister brother mother father husband wife son daughter child with me",
          "You are welcome to bring a family member or friend to your visits. They can help you remember what the team tells you."),
    Entry("getting-there", "Getting to the centre", "location where address find directions traffic open hours days weekdays getting there",
          "The centre is open on weekdays from 8 in the morning to 4 in the afternoon. Plan your trip early, as traffic in Ikeja can be heavy."),
    Entry("eating", "Eating during treatment", "food eating diet nutrition appetite meals water drink",
          "Small, regular meals and plenty of water often help during treatment. Your team or a dietitian can give advice that fits you, so ask at your next visit."),
    Entry("records", "Copies of your records", "records copies report file hospital card papers documents",
          "Ask the records desk for copies of your reports. Bring your hospital card."),
)


URGENT_WORDS = {"fever", "temperature", "shivering", "bleeding", "breathing", "breathless", "vomiting", "collapsed", "unconscious", "seizure", "emergency", "chest"}


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in STOP]


def search(query: str, limit: int = 3, minimum: float = 2.0) -> list[dict]:
    """The entries that answer a question, best first. Words from an entry's own keywords and title count most; an entry below the minimum is not an answer."""
    asked = set(words(query))
    scored = []
    for e in ENTRIES:
        keys, title, body = set(words(e.keywords)), set(words(e.title)), set(words(e.answer))
        score = 2.0 * len(asked & keys) + 1.5 * len(asked & title) + 0.5 * len(asked & body)
        if score >= minimum:
            scored.append((score, e))
    scored.sort(key=lambda pair: -pair[0])
    if asked & URGENT_WORDS:   # a warning sign in the question: the urgent-signs entry comes first, whatever else matched
        urgent = next((e for e in ENTRIES if e.id == "urgent-signs"), None)
        scored = [(99.0, urgent)] + [(s, e) for s, e in scored if e is not urgent]
    return [{"id": e.id, "title": e.title, "answer": e.answer, "score": round(score, 2), "source": SOURCE} for score, e in scored[:limit]]
