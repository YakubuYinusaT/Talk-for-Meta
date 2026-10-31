# Talk: essential services, from the phone people already have

Healthcare, emergency assistance and public information increasingly assume a smartphone, reliable internet, digital literacy and the physical ability to navigate a screen. Millions of people have none of them. Talk turns the telephone number a person already uses into an access point: they call, say what they need, and Talk moves the request into action. No app, no data plan, no smartphone.

Built by Connected Intelligence for the Meta Global AI Developer Hackathon (Indie track). Talk is the product; **SOFA** is the agent underneath that understands requests, keeps context, retrieves information and coordinates the institutions and people involved.

## The problem

In Nigerian community healthcare many facilities still run on paper records and handwritten registers. Patients travel to a hospital just to book an appointment, ask a question or follow up on their care, and some facilities have no computer at all. Information is fragmented, patients struggle to reach care, and health workers carry the administrative load. People with disabilities, older people, people with limited literacy and patients whose condition makes screens hard are hit hardest. Having a telephone does not mean having the information, the people or the services you need at a critical moment.

## What Talk does in healthcare

| Talk... | Today |
|---|---|
| **Coordinates appointments and enquiries**: finds the nearest clinic, hospital or pharmacy with its wait and fee, books and cancels, checks whether a medicine is in stock | Live, against simulated providers |
| **Gives approved information**: a caller's basic question is answered only from the facility's own patient guide, and a warning sign is always answered first, exactly as the facility wrote it | Live, with one facility's sample guide |
| **Manages follow-up**: a reminder the day before, a thank-you to the patient who came, an offer to rebook to the one who did not, and the care team's replies texted back | Live |
| **Routes requests to the right people**: a booking goes to the provider the caller chose; a question the guide does not cover goes to that facility's staff on their own front desk | Live |
| **Escalates what needs a person**: emergency words get the emergency number (112) at once, the nearest emergency care is told on its front desk, urgent first, and a text can go to staff | Live |
| **Extends to government services, public information and emergency coordination** | Next |

Clinical decisions stay with qualified people. Talk gives general information and coordinates care; it does not diagnose. Talk speaks English today: Meta's speech model has no Yoruba, Hausa, Igbo or Pidgin, so serving callers in those languages needs a model that hears them.

## Why community healthcare first

Two community hospitals have given us letters of intent to deploy Talk. In those conversations we found they lack even basic computing to organise their work, and putting AI into a facility that cannot organise its own information would solve only part of the problem. The deployment has three parts:

1. **Digital infrastructure**: the essential computers and internet connection a participating facility does not have.
2. **Information organisation**: helping the facility structure its existing operational and patient-service information so that authorised staff and AI systems can work with it. Where a facility has no system, the appointment book Talk keeps for it is the system.
3. **AI-enabled coordination**: Talk, supporting appointment management, enquiries, follow-up, request routing and administrative workflows.

Healthcare workers and trained front-desk assistants stay at the centre: Talk reduces their coordination burden and shows them what patients are asking for. The code in this repository is the third part. The first two are work in the facilities, not software. Until the hospitals are connected, this build runs against four simulated provider systems (state clinics, private clinics, a pharmacy chain, a cancer centre) built to the same contract a real provider uses, with made-up places, queues, stock and patients.

## How it works

```
caller ── phone network ── telephony provider ─┬─ callback path: record, then reply        (sofa/routes/voice.py)
                                               └─ SIP trunk → Asterisk → streamed agent    (sofa/sip)
                                                         │
                                       Muse Voice Transcribe  (what was said)
                                       Muse Spark 1.3         (what it means: strict JSON, grounded in the caller's own words)
                                                         │
                                       SOFA health front desk (sofa/gateway/adapters/health.py)
                                                         │  signed requests, one secret per provider
                          ┌───────────────┬──────────────┼───────────────┬───────────────┐
                       state-phc       eko-care       everwell        onc-centre
                     state clinics   private clinics   pharmacies   cancer centre (pilot)
                                                         │
                          each provider's front desk: appointment book, queue, requests for its staff
```

- **Talk holds no hospital records.** Each provider runs its own system and returns structured facts over a signed connection; Talk composes the wording.
- **Meta models do real work.** Muse Voice Transcribe hears the caller, with keyword biasing for place and drug names. Muse Spark 1.3 reads the request by meaning and picks among options when rules cannot, using strict JSON output and prompt caching.
- **Streamed calls** (`sofa/sip`): Asterisk answers the trunk's call and hands it to an agent that hears where the caller stops speaking, plays each reply sentence by sentence as soon as it is voiced, lets the caller talk over Talk, and reads keypad digits. It shares its turn code with the callback path.
- **The voice** is a hosted Nigerian-accent English voice, with an optional voice that runs on the machine itself (Piper, set with `PIPER_MODEL`) as a back-up for when the hosted one is out of reach.
- **Requests for a person** (`sofa/health/store.py`, `provider.py`): a question the guide does not cover, or an emergency, becomes a request on the provider's own front desk. Staff answer it there and the answer is texted to the caller.
- **Follow-up** (`sofa/health/followups.py`): reminders within 24 hours, status sync with the clinic, thank-you and we-missed-you texts, and the staff's answers, sent between 07:00 and 20:00 Lagos time and once each.

## Privacy and data handling

- A provider never receives a phone number. It sees a reference that is different for each provider, plus what the caller said to be passed on.
- Talk tells a new caller, once and right after its first answer, that calls are recorded. Recordings are deleted after 30 days (`RECORDING_RETENTION_DAYS`, 0 keeps them); the call record keeps what was said as text, the bookings and the requests.
- A recording is downloaded only over https from the telephony provider's own hosts (`RECORDING_ALLOWED_HOSTS`).
- Texts name a place by a discreet name, never by what is treated there.
- Secrets that ship with the code are never accepted on a public address: on an https address any that is left at its published default is replaced by a random one at start, and the page it guards stays shut until the real one is set.
- The public `/demo` page shows counts and what happened in recent calls. What callers said is shown only if `DEMO_SHOW_CALLS=true`.
- Every hospital, pharmacy and patient in this build is simulated.

Live explainer, provider front desks and demo page: https://talk.cohandle.app

## Run it

```bash
pip install -e ".[dev]"
python -m pytest        # the whole suite, including full calls against the four simulated providers
```

To serve it, set at least:

```
SYNTHETIC_HOSPITAL=true
HOSPITAL_BASE_URL=https://<your public address>/synthetic
SERVICES_ENABLED=lookup,health
ENABLED_LANGUAGES=en
GATEWAY_NUMBER=<the number callers dial>
LLM_PROVIDER=meta            LLM_URL=https://api.meta.ai   LLM_MODEL=muse-spark-1.3   LLM_API_KEY=<Meta Model API key>
ASR_PROVIDER=meta            ASR_URL=https://api.meta.ai
AT_CALLBACK_SECRET=<long random text>
HOSPITAL_SIGNING_SECRET=<long random text>   # each provider's own secret is derived from it
ADMIN_TOKEN=<long random text>               # the team console; PROVIDER_TOKEN and VENDOR_TOKEN likewise if those desks are used
```

then `uvicorn sofa.main:create_app --factory --port 8000`. Put the callback address `https://<your public address>/voice/inbound/<AT_CALLBACK_SECRET>` on the number in your telephony provider's dashboard. `scripts/meta_check.py` tests the Meta key and models without printing the key. To take calls over a SIP trunk instead, see `deploy/setup_sip.sh`; to prepare the audio of the fixed phrases, `python -m scripts.warm_cache`; for the back-up voice, `deploy/setup_voice.sh`.

## Where things are

| | |
|---|---|
| `sofa/health/` | the four simulated provider systems, their data, the patient guide, the follow-ups |
| `sofa/gateway/adapters/health.py` | the health front desk that orchestrates the providers |
| `sofa/sip/` | the streamed-call agent (AudioSocket protocol, speech detection, the call itself) |
| `sofa/clients/` | Meta language model and speech, the voices |
| `sofa/routes/voice.py` | the callback path and the turn code both paths share |
| `sofa/web/`, `sofa/routes/overview.py` | the explainer site and the provider front desks |
| `deploy/` | set-up scripts for the SIP trunk (Asterisk) and the voice on the machine itself; they expect the app under `/opt/sofa-health` |
| `docs/design.md` | the design; `docs/model_log.md` the log of every model issue and fix |

Tests live in `tests/`; the stand-in speech and language models that let them run offline are in `mocks/`.
