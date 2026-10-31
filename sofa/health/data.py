"""The made-up health network: areas of Lagos, three independent provider systems (a state health network, a private clinic group and a pharmacy chain),
and a small drug list. No real facility, number, price or stock level is used.

Area positions are rough, so a caller in Surulere is told the nearest places first. Facility names and prices are invented. Each provider system runs on its
own, with its own records and its own secret, and Talk reaches each one through its own connector.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Area:
    name: str
    lga: str
    lat: float
    lon: float
    aliases: tuple[str, ...] = ()


AREAS = {a.name: a for a in (
    Area("Surulere", "Surulere", 6.5010, 3.3580, ("aguda", "bode thomas", "adeniran ogunsanya")),
    Area("Yaba", "Lagos Mainland", 6.5095, 3.3711, ("akoka", "sabo", "onike")),
    Area("Ikeja", "Ikeja", 6.6018, 3.3515, ("allen", "alausa", "computer village", "oregun", "ojodu")),
    Area("Maryland", "Ikeja", 6.5667, 3.3667, ("anthony", "mende")),
    Area("Gbagada", "Kosofe", 6.5575, 3.3919, ("soluyi", "ifako")),
    Area("Ojota", "Kosofe", 6.5833, 3.3833, ("ketu", "ogudu", "mile 12")),
    Area("Mushin", "Mushin", 6.5272, 3.3560, ("idi oro", "ladipo", "papa ajao")),
    Area("Oshodi", "Oshodi-Isolo", 6.5550, 3.3433, ("isolo", "ejigbo", "ilasamaja")),
    Area("Agege", "Agege", 6.6200, 3.3200, ("pen cinema", "iju", "ogba")),
    Area("Alimosho", "Alimosho", 6.5866, 3.2631, ("egbeda", "idimu", "ipaja", "iyana ipaja", "ikotun", "igando")),
    Area("Apapa", "Apapa", 6.4489, 3.3592, ("ijora", "ajegunle", "tincan")),
    Area("Festac", "Amuwo-Odofin", 6.4647, 3.2822, ("amuwo odofin", "satellite town", "mile 2")),
    Area("Ebute Metta", "Lagos Mainland", 6.4833, 3.3833, ("oyingbo", "iddo")),
    Area("Lagos Island", "Lagos Island", 6.4550, 3.3950, ("marina", "idumota", "balogun", "obalende")),
    Area("Victoria Island", "Eti-Osa", 6.4281, 3.4219, ("vi", "oniru", "ahmadu bello")),
    Area("Ikoyi", "Eti-Osa", 6.4500, 3.4333, ("falomo", "banana island")),
    Area("Lekki", "Eti-Osa", 6.4478, 3.4723, ("lekki phase 1", "phase one", "chevron", "jakande", "osapa")),
    Area("Ajah", "Eti-Osa", 6.4698, 3.5852, ("sangotedo", "abraham adesanya", "thomas estate", "badore")),
    Area("Ikorodu", "Ikorodu", 6.6194, 3.5105, ("ikorodu garage", "igbogbo", "ebute")),
    Area("Badagry", "Badagry", 6.4156, 2.8876, ("ajara",)),
    Area("Epe", "Epe", 6.5840, 3.9830, ("epe town",)),
)}


@dataclass(frozen=True)
class Facility:
    id: str
    name: str
    kind: str            # primary health centre | general hospital | clinic | pharmacy
    area: str
    address: str
    offset: tuple[float, float]   # small shift from the area's centre, so two places in one area are not on the same spot
    services: tuple[str, ...]
    hours: dict          # weekday (0 is Monday) -> (opens, closes) as whole hours; a missing day is closed
    emergency: bool = False       # open at all hours for emergencies
    booking: bool = True
    base_queue: int = 10          # people waiting at the busiest quiet hour
    minutes: int = 8              # minutes the clinic takes per patient
    fee: int = 0                  # what a consultation costs, in naira (made up)
    capacity: int = 2             # patients it sees in one appointment slot (a clinic that gives each patient a fixed time has 1)
    specialist: bool = False      # a specialist centre is offered only for its own need, never as a general clinic
    sms_name: str = ""            # how a text message names it when its own name would say what a patient is being treated for

    @property
    def lat(self) -> float:
        return AREAS[self.area].lat + self.offset[0]

    @property
    def lon(self) -> float:
        return AREAS[self.area].lon + self.offset[1]


@dataclass(frozen=True)
class Network:
    """One provider system: who runs it and what it holds. Talk meets it only through its connector."""
    slug: str
    name: str            # the organisation
    owner: str           # how a caller hears it described: "state", "private" or "pharmacy chain"
    facilities: tuple[Facility, ...]


def weekdays(opens: int, closes: int) -> dict:
    return {d: (opens, closes) for d in range(5)}


PHC_HOURS = {**weekdays(8, 16), 5: (9, 13)}
CLINIC_HOURS = {**weekdays(8, 18), 5: (9, 15)}
HOSPITAL_HOURS = {**weekdays(8, 17), 5: (8, 14)}
PHARMACY_HOURS = {d: (8, 21) for d in range(7)}
ALL_NIGHT = {d: (0, 24) for d in range(7)}

GENERAL = "general consultation"
ANTENATAL = "antenatal care"
IMMUNISATION = "child immunisation"
MALARIA = "malaria test"
FAMILY = "family planning"
LAB = "laboratory tests"
EMERGENCY = "emergency care"
ONCOLOGY = "cancer care"

STATE = Network("state-phc", "Lagos State Primary Health Network", "state", (
    Facility("hc-aguda", "Aguda Community Health Centre", "primary health centre", "Surulere", "Bode Thomas Street", (0.004, -0.003),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA, FAMILY), PHC_HOURS, base_queue=14, minutes=7, fee=500),
    Facility("hc-yaba-mainland", "Yaba Mainland Health Centre", "primary health centre", "Yaba", "Murtala Muhammed Way", (0.003, 0.001),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA), PHC_HOURS, base_queue=13, minutes=7, fee=500),
    Facility("gh-ikeja-heights", "Ikeja Heights General Hospital", "general hospital", "Ikeja", "Obafemi Awolowo Way", (0.003, 0.002),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA, LAB, FAMILY, EMERGENCY), HOSPITAL_HOURS, emergency=True, base_queue=30, minutes=6, fee=1000),
    Facility("hc-mushin-unity", "Mushin Unity Health Centre", "primary health centre", "Mushin", "Ladipo Road", (0.002, 0.003),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA), PHC_HOURS, base_queue=16, minutes=7, fee=500),
    Facility("hc-oshodi-crossroads", "Oshodi Crossroads Health Centre", "primary health centre", "Oshodi", "Agege Motor Road", (-0.003, 0.002),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA, FAMILY), PHC_HOURS, base_queue=18, minutes=7, fee=500),
    Facility("hc-gbagada", "Gbagada Community Health Centre", "primary health centre", "Gbagada", "Soluyi Road", (0.001, -0.002),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA, FAMILY), PHC_HOURS, base_queue=12, minutes=7, fee=500),
    Facility("hc-alimosho-hope", "Alimosho Hope Health Centre", "primary health centre", "Alimosho", "Egbeda Road", (0.002, -0.002),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA), PHC_HOURS, base_queue=20, minutes=8, fee=500),
    Facility("hc-agege", "Agege Pen Cinema Health Centre", "primary health centre", "Agege", "Old Abeokuta Road", (0.001, 0.002),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA), PHC_HOURS, base_queue=15, minutes=7, fee=500),
    Facility("gh-ikorodu-greenfield", "Ikorodu Greenfield General Hospital", "general hospital", "Ikorodu", "Lagos Road", (0.002, 0.001),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA, LAB, EMERGENCY), HOSPITAL_HOURS, emergency=True, base_queue=26, minutes=6, fee=1000),
    Facility("hc-festac-town", "Festac Town Health Centre", "primary health centre", "Festac", "Second Avenue", (0.002, 0.002),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA), PHC_HOURS, base_queue=15, minutes=7, fee=500),
    Facility("gh-island-central", "Lagos Island Central Hospital", "general hospital", "Lagos Island", "Broad Street", (0.001, -0.001),
             (GENERAL, ANTENATAL, MALARIA, LAB, EMERGENCY), HOSPITAL_HOURS, emergency=True, base_queue=32, minutes=6, fee=1000),
    Facility("hc-ajah", "Ajah Community Health Centre", "primary health centre", "Ajah", "Sangotedo Road", (0.003, 0.001),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA), PHC_HOURS, base_queue=14, minutes=7, fee=500),
    Facility("hc-apapa-marine", "Apapa Marine Health Centre", "primary health centre", "Apapa", "Creek Road", (0.002, -0.001),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA), PHC_HOURS, base_queue=12, minutes=7, fee=500),
))

PRIVATE = Network("eko-care", "Eko Care Private Clinics", "private", (
    Facility("cl-surulere-care", "Surulere Care Clinic", "clinic", "Surulere", "Adeniran Ogunsanya Street", (-0.003, 0.002),
             (GENERAL, ANTENATAL, MALARIA, LAB, FAMILY), CLINIC_HOURS, base_queue=6, minutes=10, fee=8000),
    Facility("cl-yaba-lakeview", "Yaba Lakeview Clinic", "clinic", "Yaba", "Herbert Macaulay Way", (-0.002, 0.004),
             (GENERAL, MALARIA, LAB, FAMILY), CLINIC_HOURS, base_queue=7, minutes=9, fee=7000),
    Facility("cl-ikeja-allen", "Ikeja Allen Medical Centre", "clinic", "Ikeja", "Allen Avenue", (-0.002, 0.003),
             (GENERAL, ANTENATAL, IMMUNISATION, MALARIA, LAB, FAMILY), CLINIC_HOURS, base_queue=8, minutes=10, fee=10000),
    Facility("cl-gbagada-heights", "Gbagada Heights Clinic", "clinic", "Gbagada", "Diya Street", (-0.002, 0.002),
             (GENERAL, ANTENATAL, MALARIA, LAB), CLINIC_HOURS, base_queue=5, minutes=10, fee=8000),
    Facility("cl-mushin-grace", "Mushin Grace Clinic", "clinic", "Mushin", "Ojuwoye Road", (-0.002, -0.002),
             (GENERAL, MALARIA, IMMUNISATION), CLINIC_HOURS, base_queue=6, minutes=9, fee=6000),
    Facility("cl-lekki-family", "Lekki Family Clinic", "clinic", "Lekki", "Admiralty Way", (0.001, 0.002),
             (GENERAL, ANTENATAL, IMMUNISATION, LAB, FAMILY), CLINIC_HOURS, base_queue=5, minutes=12, fee=15000),
    Facility("cl-ajah-seaside", "Ajah Seaside Medical Centre", "clinic", "Ajah", "Lekki-Epe Expressway", (0.002, -0.002),
             (GENERAL, MALARIA, LAB, EMERGENCY), CLINIC_HOURS, emergency=True, base_queue=8, minutes=10, fee=12000),
    Facility("cl-vi-wellness", "Victoria Island Wellness Clinic", "clinic", "Victoria Island", "Adeola Odeku Street", (0.001, 0.001),
             (GENERAL, ANTENATAL, LAB, FAMILY), CLINIC_HOURS, base_queue=4, minutes=12, fee=20000),
    Facility("cl-apapa-harbour", "Apapa Harbour Clinic", "clinic", "Apapa", "Warehouse Road", (0.001, 0.001),
             (GENERAL, MALARIA, IMMUNISATION), CLINIC_HOURS, base_queue=6, minutes=9, fee=7000),
    Facility("cl-ikorodu-greenfield", "Ikorodu Greenfield Clinic", "clinic", "Ikorodu", "Sagamu Road", (-0.002, 0.002),
             (GENERAL, ANTENATAL, MALARIA), CLINIC_HOURS, base_queue=6, minutes=10, fee=6000),
))

PHARMACIES = Network("everwell", "Everwell Pharmacies", "pharmacy chain", (
    Facility("ph-everwell-surulere", "Everwell Pharmacy Surulere", "pharmacy", "Surulere", "Adeniran Ogunsanya Street", (-0.002, 0.003),
             ("medicines", "blood pressure check"), PHARMACY_HOURS, booking=False, base_queue=3, minutes=4),
    Facility("ph-yaba-wellness", "Everwell Pharmacy Yaba", "pharmacy", "Yaba", "Commercial Avenue", (0.003, -0.002),
             ("medicines",), PHARMACY_HOURS, booking=False, base_queue=2, minutes=4),
    Facility("ph-ikeja-central", "Everwell Pharmacy Ikeja", "pharmacy", "Ikeja", "Allen Avenue", (-0.002, 0.004),
             ("medicines", "blood pressure check"), ALL_NIGHT, booking=False, base_queue=4, minutes=4),
    Facility("ph-lekki-plus", "Everwell Pharmacy Lekki", "pharmacy", "Lekki", "Admiralty Way", (-0.001, 0.003),
             ("medicines",), PHARMACY_HOURS, booking=False, base_queue=2, minutes=4),
    Facility("ph-mushin-care", "Everwell Pharmacy Mushin", "pharmacy", "Mushin", "Idi Oro Road", (0.001, -0.002),
             ("medicines",), PHARMACY_HOURS, booking=False, base_queue=3, minutes=4),
    Facility("ph-gbagada", "Everwell Pharmacy Gbagada", "pharmacy", "Gbagada", "Ifako Road", (0.001, 0.001),
             ("medicines",), PHARMACY_HOURS, booking=False, base_queue=2, minutes=4),
))

CANCER = Network("onc-centre", "Oncology Care Centre Ikeja (simulated pilot partner)", "state", (
    Facility("onc-ikeja", "Oncology Care Centre Ikeja", "cancer centre", "Ikeja", "Oba Akran Avenue", (0.005, -0.004),
             (ONCOLOGY, "chemotherapy day unit", "radiotherapy review", "cancer counselling"), weekdays(8, 16), base_queue=4, minutes=5, fee=1000, capacity=1, specialist=True,
             sms_name="the Ikeja care centre"),
))

NETWORKS = {n.slug: n for n in (STATE, PRIVATE, PHARMACIES, CANCER)}
FACILITIES: tuple[Facility, ...] = tuple(f for n in NETWORKS.values() for f in n.facilities)


@dataclass(frozen=True)
class Drug:
    id: str
    name: str            # as a person says it
    form: str
    price: int           # naira for one pack, made up
    prescription: bool = False
    aliases: tuple[str, ...] = ()


DRUGS: tuple[Drug, ...] = (
    Drug("paracetamol", "Paracetamol 500 milligram tablets", "tablets", 400, aliases=("panadol", "paracetamol")),
    Drug("ibuprofen", "Ibuprofen 400 milligram tablets", "tablets", 650, aliases=("ibuprofen", "brufen")),
    Drug("act", "Artemether and lumefantrine tablets for malaria", "tablets", 2200, prescription=True, aliases=("coartem", "artemether", "lumefantrine", "malaria tablets", "malaria drug", "act")),
    Drug("ors", "Oral rehydration salts", "sachets", 250, aliases=("ors", "oral rehydration", "rehydration salts")),
    Drug("zinc", "Zinc 20 milligram dispersible tablets", "tablets", 500, aliases=("zinc",)),
    Drug("folic", "Folic acid 5 milligram tablets", "tablets", 600, aliases=("folic acid", "folate", "folic")),
    Drug("iron", "Iron and folic acid tablets", "tablets", 900, aliases=("iron", "ferrous", "iron tablets", "blood tonic", "fefol")),
    Drug("vitc", "Vitamin C 500 milligram tablets", "tablets", 700, aliases=("vitamin c", "vit c")),
    Drug("multivit", "Multivitamin tablets", "tablets", 1500, aliases=("multivitamin", "multi vitamin", "vitamins")),
    Drug("amoxicillin", "Amoxicillin 500 milligram capsules", "capsules", 1800, prescription=True, aliases=("amoxicillin", "amoxil", "amoxicilin")),
    Drug("cotrimoxazole", "Cotrimoxazole tablets", "tablets", 1200, prescription=True, aliases=("cotrimoxazole", "septrin", "co trimoxazole")),
    Drug("metformin", "Metformin 500 milligram tablets", "tablets", 1400, prescription=True, aliases=("metformin", "glucophage")),
    Drug("amlodipine", "Amlodipine 5 milligram tablets", "tablets", 1300, prescription=True, aliases=("amlodipine", "amlo")),
    Drug("salbutamol", "Salbutamol inhaler", "inhaler", 3200, prescription=True, aliases=("salbutamol", "ventolin", "inhaler")),
    Drug("loratadine", "Loratadine 10 milligram tablets", "tablets", 800, aliases=("loratadine", "claritin", "allergy tablets")),
    Drug("omeprazole", "Omeprazole 20 milligram capsules", "capsules", 1500, aliases=("omeprazole", "ulcer tablets")),
    Drug("pills", "Contraceptive pills", "pack", 1800, aliases=("contraceptive", "family planning pills", "the pill", "pills")),
    Drug("condom", "Condoms", "pack", 500, aliases=("condom", "condoms")),
    Drug("sulphadoxine", "Sulphadoxine and pyrimethamine tablets", "tablets", 900, prescription=True, aliases=("sp tablets", "sulphadoxine", "fansidar")),
    Drug("bpmonitor", "Blood pressure monitor", "device", 18000, aliases=("blood pressure monitor", "bp monitor")),
)


def stock_level(facility_id: str, drug_id: str) -> int:
    """How many packs a pharmacy holds: made up but fixed, so a demo gives the same answer twice. Some are out of stock on purpose."""
    seed = sum(ord(c) for c in f"{facility_id}|{drug_id}")
    return 0 if seed % 7 == 0 else 5 + seed % 40
