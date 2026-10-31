# Talk for Meta: design

Talk is a phone number that gets people to care in Lagos. This page says what it does, how a call is handled, which Meta models do which job, and what is simulated.

## The problem

In most hospitals a patient has to come in and queue. Some hospitals give fixed appointment times, but on paper-and-pen books, and few have a computer at the desk. So there is nothing for a phone call to book into, and nobody follows up the patients who were seen or the ones who never came.

Talk books by call, writes into the hospital's appointment book (it keeps one for a hospital that has none), and follows up both the patients who were attended and those still to come.

## The pilot

Two community hospitals have given us letters of intent to deploy Talk, and they lack even basic computing to organise their work. So the deployment has three parts: digital infrastructure (computers and connectivity), information organisation (structuring the facility's existing operational and patient-service information), and AI-enabled coordination, which is Talk. Healthcare workers and trained front-desk assistants stay at the centre of the model, and clinical decisions stay with qualified people. This repository is the third part.

Until the hospitals are connected, the build runs against four simulated provider systems built to the same contract a real one will use. One of them stands in for a specialist cancer centre: a fixed time for every patient, a patient guide, a record of who came, and follow-up. The guide is sample content, labelled as such.

## What a caller can do

- Find care near them for a need such as antenatal care, immunisation or a malaria test, with opening hours, waiting time and fee. Talk puts the real options in front of the caller and asks which one: an area is not a hospital, so Talk never picks for them.
- Book and cancel with whichever provider holds the visit. Talk asks only for what the caller has not already said, reads the time back, and books only on a clear yes.
- Check a medicine: which pharmacies stock it, what a pack costs, and whether it needs a prescription.
- Ask the cancer centre a basic question. The answer comes only from the centre's own patient guide. A warning sign is always answered first and exactly as the centre wrote it. A question the guide does not cover goes to the care team and is never guessed at.
- Receive a reminder the day before, and after the visit a thank-you if the patient came, or an offer to rebook if they did not.
- Have a question or an emergency passed to a person. It appears on the provider's own front desk, urgent first. Staff answer it there, and the answer is texted to the caller.
- Emergencies: words such as "not breathing" or "collapsed" get the emergency number, 112, at once, from any point in a call, and then the nearest emergency care. Talk gives general information and coordinates care. It does not diagnose.

## How a call is handled

1. **The call.** The caller dials one number. Either the telephony provider records each thing the caller says and asks Talk what to play next (the callback path), or a SIP trunk delivers the call to a streamed agent that hears the caller as they speak (`sofa/sip`). Both paths share the same turn code.
2. **Hearing.** Muse Voice Transcribe turns the speech into text. Talk gives it the names of Lagos areas, clinics and medicines to listen for.
3. **Understanding.** Muse Spark 1.3 reads the sentence by meaning and returns structured data in a strict JSON format: what the caller needs, where they are, when they want it. A place, a time or a medicine only counts if it is made of the caller's own words. The model never supplies a fact.
4. **The front desk.** Talk's health front desk (`sofa/gateway/adapters/health.py`) asks every provider that could help, at the same moment, with a signed request, and puts the real options in front of the caller with distance, waiting time and fee.
5. **The booking.** Talk asks the chosen provider for a free time, reads it back, and books it on a clear yes. It says the visit is booked only when the provider's system confirms it. The details are texted to the caller.
6. **The reply.** The reply is spoken one sentence at a time, so Talk starts speaking when the first sentence is ready. On a streamed call the caller can talk over Talk, and Talk stops.
7. **Follow-up.** A job (`sofa/health/followups.py`) sends the reminder, checks the clinic's own book after the visit, and sends the thank-you or the we-missed-you text. Texts are sent between 07:00 and 20:00 Lagos time and once each.
8. **A person.** A question the patient guide does not cover becomes a request on the cancer centre's front desk. An emergency becomes an urgent request on the nearest emergency care's front desk, and a text can go to a staff number. Staff reply on the desk, and the follow-up job texts the reply to the caller, once. A request nobody answers is dropped after a week.

## What each Meta model does

| Model | Job |
|---|---|
| Muse Voice Transcribe | Hears the caller (English, with keyword hints for places, clinics and medicines). |
| Muse Spark 1.3 | Understands the request as strict JSON, picks among the real options when the rules cannot, and words the reply. Standard mode only, never a contributor model. |

## The connected providers

The health network is four independent synthetic provider systems, each with its own records, its own secret and its own signed connection, the way real providers would be reached.

| Provider | What it is | What it offers |
|---|---|---|
| Lagos State Primary Health Network | state health centres and general hospitals | low fees, longer queues, emergency departments |
| Eko Care Private Clinics | a private clinic group | higher fees, shorter queues |
| Everwell Pharmacies | a pharmacy chain | medicines, prices, stock |
| Oncology Care Centre Ikeja (simulated pilot partner) | a cancer centre that gives each patient a fixed time | cancer care, a patient guide, attendance, follow-up |

Talk holds no hospital records. A provider answers a small set of signed requests (find places, list free times, book, cancel, report what became of a visit) and returns structured facts; Talk composes the wording. A provider with no system of its own uses the appointment book Talk keeps for it, and its front-desk page shows who is coming and marks who came.

## Privacy

- A provider never receives a phone number. It sees a reference that is different for each provider.
- A text names a place by a discreet name, never by what is treated there.
- Talk tells a new caller, once and right after its first answer, that calls are recorded. Recordings are deleted after 30 days; the call record keeps what was said as text, the bookings and the requests.
- Talk keeps the caller's number, what was said on the call and the booking details. The public demo page shows counts and what happened, with phone numbers masked, and shows what callers said only when it is switched on.
- A recording is downloaded only over https from the telephony provider's own hosts, and a secret left at the value published with the code is never accepted on a public address.
- Every hospital, pharmacy and patient in this build is simulated.

## What is next

A patient sending a photo of a prescription or a drug label through a link by text, with the pharmacist approving before anything is dispensed; repeat medicines and antenatal visits on schedule for a registered patient; realtime transcription while the caller speaks; callers in Yoruba, Hausa, Igbo and Pidgin once a speech model hears them; and the same architecture for government services, public information and emergency coordination.
