# Talk for Meta: care in Lagos, by phone call

Talk is a phone number that gets people to care. Someone calls, says what they need in plain English, and Talk finds the right clinic, books the visit in that clinic's own appointment book, and follows up afterwards: a reminder before, a thank-you to the patient who came, and an offer to rebook to the one who did not. No smartphone, no app, no internet.

Built by Connected Intelligence for the Meta Global AI Developer Hackathon (Indie track). Talk is the product; **SOFA** is the agent underneath that coordinates and orchestrates every request.

## The problem

In most Lagos hospitals a patient has to come in and queue. Some hospitals are moving to fixed appointment times, but on paper-and-pen books, and few have a computer at the desk. So there is nothing for a phone call to book into, and nobody follows up the patients who were seen or the ones who never came.

Talk is the missing layer. It books by call, writes into the hospital's appointment book (Talk keeps one for a hospital that has none), and follows up both the patients who were attended and those still to come.

## What a caller can do

- **Find care** near them for a need such as antenatal care, immunisation or a malaria test, with opening hours, waiting time and fee. Talk puts the real options in front of the caller and asks which one, because an area is not a hospital.
- **Book and cancel** with whichever provider holds the visit. Talk asks only for what the caller has not already said.
- **Check a medicine**: which pharmacies stock it, what a pack costs, and whether it needs a prescription.
- **Ask the cancer centre** basic questions, answered only from the centre's own patient guide. A warning sign is always answered first and exactly as written, and a question the guide does not cover goes to the care team.
- **Get reminders and follow-up** by text. Texts name a place by a discreet name, never by what is treated.
- **Emergencies**: words like "not breathing" get 112 immediately, from any point in a call.

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
```

- **Talk holds no hospital records.** Each provider runs its own system and returns structured facts over a signed connection; Talk composes the wording.
- **Meta models do real work**: Muse Voice Transcribe hears the caller (with keyword biasing for place and drug names), Muse Spark 1.3 reads the request by meaning and picks among options when rules cannot, using strict JSON output and prompt caching.
- **The voice** is a hosted Nigerian-accent English voice, with an optional voice that runs on the machine itself (Piper, set with `PIPER_MODEL`) as a back-up for when the hosted one is out of reach. Fixed phrases are voiced once and kept.
- **Streamed calls** (`sofa/sip`): Asterisk answers the trunk's call and hands it to an agent that hears where the caller stops speaking, plays each reply sentence by sentence as soon as it is voiced, lets the caller talk over Talk, and reads keypad digits. It shares its turn code with the callback path, so both behave alike.
- **Follow-up** (`sofa/health/followups.py`): reminders within 24 hours, status sync with the clinic, thank-you and we-missed-you texts, sent between 07:00 and 20:00 Lagos time and once each.

## What is simulated

The four provider systems are simulated: their places, queues, fees, stock and patients are made up. The cancer centre is "Oncology Care Centre Ikeja (simulated pilot partner)", with a sample patient guide that the centre's own guide will replace. They are built to the same contract a real provider uses (own records, own secret, signed requests, a front-desk page that doubles as the appointment book), so connecting a real hospital means a new connection and real data, not a new design.

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
| `docs/design.md` | the full design; `docs/model_log.md` the log of every model issue and fix |

Tests live in `tests/`; the stand-in speech and language models that let them run offline are in `mocks/`.
