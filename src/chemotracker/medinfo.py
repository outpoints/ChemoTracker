"""Medicine catalog shown during setup and on each medicine's page.

Plain-language summaries written from MedlinePlus (U.S. National Library of
Medicine) drug pages, linked per entry. It is background reading for the
family, not dosing advice: the pharmacy label and the care team come first.

`defaults` are only starting points for the setup form (route, where it is
given, and a typical pattern). Every value is editable in the app.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_NOTE = (
    "Summarised from MedlinePlus (U.S. National Library of Medicine). "
    "The pharmacy label and the care team's instructions come first."
)

CATEGORIES: list[tuple[str, str, str]] = [
    ("infusion", "Chemo & infusions", "Given through the IV at the clinic"),
    ("oral_cancer", "Cancer medicines at home", "Pills taken at home as part of treatment"),
    ("nausea", "Nausea & vomiting", "Scheduled anti-nausea medicines and rescue pills"),
    ("blood", "White-blood-cell boosters", "Shots that lower the risk of infection"),
    ("premed", "Allergy & stomach pre-meds", "Often given before an infusion"),
    ("comfort", "Bowels, pain & comfort", "Everyday side-effect helpers"),
]
CATEGORY_NAMES = {key: name for key, name, _ in CATEGORIES}

KIND_NAMES = {"scheduled": "On a schedule", "as_needed": "As needed", "clinic": "Given at the clinic"}
ROUTE_NAMES = {"oral": "By mouth", "iv": "IV", "injection": "Shot", "topical": "On the skin", "other": "Other"}


@dataclass(frozen=True)
class Defaults:
    route: str = "oral"
    kind: str = "scheduled"
    days: tuple[int, int] = (1, 1)  # end 0 = through the end of the cycle
    times: tuple[str, ...] = ()
    label: str = ""


@dataclass(frozen=True)
class MedInfo:
    key: str
    generic: str
    brands: tuple[str, ...]
    category: str
    family: str
    why: str
    how: str
    common: tuple[str, ...]
    call: tuple[str, ...]
    source: str
    tips: tuple[str, ...] = ()
    defaults: Defaults = field(default_factory=Defaults)
    chips: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        return f"{self.generic} ({self.brands[0]})" if self.brands else self.generic


CLINIC_IV = Defaults(route="iv", kind="clinic", days=(1, 1), label="At the infusion")
MED = "https://medlineplus.gov/druginfo/meds/"

_ENTRIES: list[MedInfo] = [
    # ------------------------------------------------------------ infusion
    MedInfo(
        "doxorubicin", "Doxorubicin", ("Adriamycin",), "infusion",
        "Chemotherapy (anthracycline)",
        "Treats the cancer. It is the \"A\" in AC and AC-T.",
        "Given through the IV by the infusion team.",
        ("Red or pink urine for 1 to 2 days after a dose (expected)", "Nausea or vomiting", "Mouth and throat sores",
         "Hair loss", "Tiredness or weakness", "Less appetite"),
        ("Hives, rash, itching, or trouble breathing or swallowing", "Pain, redness, swelling, or blisters where the IV went in",
         "Fever, chills, sore throat, or other signs of infection", "Unusual bleeding or bruising, or black, tarry stools"),
        MED + "a682221.html",
        tips=("The care team checks heart function before and during treatment.",),
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "epirubicin", "Epirubicin", ("Ellence",), "infusion",
        "Chemotherapy (anthracycline)",
        "Treats breast cancer after surgery, together with other medicines.",
        "Given through the IV by the infusion team.",
        ("Nausea or vomiting", "Mouth and throat sores", "Diarrhea or stomach pain", "Less appetite",
         "Unusual tiredness or weakness", "Hair loss"),
        ("Pale skin, fainting, or dizziness", "Rash, hives, or itching", "Fever or other signs of infection"),
        MED + "a603003.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "cyclophosphamide", "Cyclophosphamide", ("Cytoxan",), "infusion",
        "Chemotherapy (alkylating agent)",
        "Treats the cancer. It is the \"C\" in AC, TC, and CMF.",
        "Given through the IV by the infusion team. Drink plenty of fluids and urinate often to protect the bladder.",
        ("Nausea, vomiting, or diarrhea", "Less appetite", "Hair loss", "Mouth or tongue sores", "Changes in skin or nail color"),
        ("Pink or red urine, blood in the urine, or frequent, urgent, or painful urination",
         "Sore throat, fever, chills, or other signs of infection", "Unusual bruising or bleeding, black tarry stools, or nosebleeds",
         "New cough, chest pain, or shortness of breath", "Swelling of the legs or ankles, or a fast or irregular heartbeat"),
        MED + "a682080.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "paclitaxel", "Paclitaxel", ("Taxol",), "infusion",
        "Chemotherapy (taxane)",
        "Treats the cancer. It is the \"T\" in AC-T, often given after the AC cycles.",
        "Given through the IV. Medicines are given beforehand to help prevent an allergic reaction.",
        ("Numbness, burning, or tingling in the hands or feet", "Muscle or joint pain", "Nausea, vomiting, or diarrhea",
         "Mouth or lip sores", "Hair loss"),
        ("Rash, hives, itching, swelling, or trouble breathing (allergic reaction)", "Fever, chills, sore throat, or other signs of infection",
         "Shortness of breath, chest pain, or a slow or irregular heartbeat", "Unusual bruising or bleeding, pale skin, or extreme tiredness"),
        MED + "a607070.html",
        tips=("Tell the team early about tingling or numbness. They track it from visit to visit.",),
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "nab-paclitaxel", "Paclitaxel protein-bound", ("Abraxane",), "infusion",
        "Chemotherapy (taxane, albumin-bound)",
        "Treats certain breast, lung, and pancreatic cancers.",
        "Given through the IV by the infusion team.",
        ("Tiredness or weakness", "Nausea, vomiting, or diarrhea", "Mouth or throat sores", "Hair loss",
         "Swelling of the hands, feet, or ankles", "Pain or redness where the IV went in"),
        ("Numbness, burning, or tingling in the hands or feet", "A dry cough that won't go away, or shortness of breath",
         "Rash, hives, itching, swelling, or trouble breathing", "Pale skin, extreme tiredness, dizziness, or fainting",
         "Unusual bruising or bleeding"),
        MED + "a619008.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "docetaxel", "Docetaxel", ("Taxotere",), "infusion",
        "Chemotherapy (taxane)",
        "Treats certain breast, lung, prostate, stomach, and head and neck cancers. It is the \"T\" in TC and TCHP.",
        "Given through the IV. Steroid pills are often prescribed around infusion day; follow the label.",
        ("Vomiting or constipation", "Taste changes", "Extreme tiredness", "Muscle, joint, or bone pain", "Hair loss",
         "Nail changes or watery eyes"),
        ("Blistering skin", "Numbness, tingling, burning, or weakness in the hands or feet",
         "Unusual bleeding, bruising, or nosebleeds", "Blurred vision", "Fever or other signs of infection"),
        MED + "a696031.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "carboplatin", "Carboplatin", ("Paraplatin",), "infusion",
        "Chemotherapy (platinum)",
        "Treats several cancers, including some breast and ovarian cancers.",
        "Given through the IV by the infusion team.",
        ("Nausea or vomiting", "Diarrhea or constipation", "Mouth and throat sores", "Pain, burning, or tingling in the hands or feet",
         "Hair loss"),
        ("Pale skin, unusual tiredness, fainting, or dizziness", "Sudden changes in vision, including color vision",
         "Peeing less than usual", "Rash, hives, itching, or trouble breathing", "Fever or other signs of infection"),
        MED + "a695017.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "cisplatin", "Cisplatin", ("Platinol",), "infusion",
        "Chemotherapy (platinum)",
        "Treats several cancers, often together with other medicines.",
        "Given through the IV with extra fluids.",
        ("Nausea or vomiting", "Diarrhea", "Hair loss", "Food tasting different", "Hiccups",
         "Dry mouth, dark urine, or dry skin (signs of dehydration)"),
        ("Pain, burning, or tingling in the hands or feet", "Muscle cramps or trouble walking", "Ringing in the ears or hearing loss",
         "Swelling, pain, or burning where the IV went in", "Seizures"),
        MED + "a684036.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "fluorouracil", "Fluorouracil (5-FU)", ("Adrucil",), "infusion",
        "Chemotherapy (antimetabolite)",
        "Treats several cancers, including some breast cancers (the \"F\" in CMF-type plans).",
        "Given through the IV by the infusion team. Some plans send it home in a small pump; follow the team's instructions.",
        ("Less appetite or nausea", "Tiredness or weakness", "Dizziness or headache", "Hair loss", "Dry, cracked skin"),
        ("Mouth or throat sores", "Diarrhea or vomiting", "Swelling, pain, redness, or peeling on the palms or soles",
         "Fever, chills, sore throat, or other signs of infection", "Hives"),
        MED + "a682708.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "methotrexate", "Methotrexate", ("Trexall",), "infusion",
        "Chemotherapy (antimetabolite)",
        "Used in some cancer plans (the \"M\" in CMF) and other conditions.",
        "Given through the IV or as an injection, depending on the plan.",
        ("Joint or muscle pain", "Red eyes", "Swollen gums", "Hair loss"),
        ("Vomiting", "Blurred vision or sudden vision loss", "Sudden fever with a severe headache and stiff neck",
         "Seizures, confusion, or weakness on one side", "Mouth sores, diarrhea, or signs of infection"),
        MED + "a682018.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "gemcitabine", "Gemcitabine", ("Gemzar",), "infusion",
        "Chemotherapy (antimetabolite)",
        "Treats several cancers, including some breast, ovarian, lung, and pancreatic cancers.",
        "Given through the IV by the infusion team.",
        ("Nausea or vomiting", "Diarrhea or constipation", "Less appetite", "Mouth and throat sores", "Hair loss", "Headache"),
        ("Rash, hives, swelling, or trouble breathing", "Redness, blistering, or peeling skin with fever",
         "Unusual bleeding or bruising, or black, tarry stools", "A change in how much you urinate",
         "Fever, sore throat, cough, or other signs of infection"),
        MED + "a696019.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "eribulin", "Eribulin", ("Halaven",), "infusion",
        "Chemotherapy (microtubule inhibitor)",
        "Treats breast cancer that has spread and was treated with other chemotherapy before.",
        "Given through the IV by the infusion team.",
        ("Nausea or constipation", "Less appetite or weight loss", "Headache", "Weakness or tiredness", "Bone, back, or joint pain"),
        ("Fever, chills, sore throat, cough, or burning when urinating", "Numbness, burning, or tingling in the arms, legs, hands, or feet",
         "Pale skin or shortness of breath", "Irregular heartbeat"),
        MED + "a611007.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "trastuzumab", "Trastuzumab", ("Herceptin", "Kanjinti", "Ogivri", "Herzuma"), "infusion",
        "Targeted therapy (HER2 antibody)",
        "Treats HER2-positive breast and stomach cancers. The \"H\" in TCH and TCHP.",
        "Given through the IV or as a shot under the skin, usually every 1 to 3 weeks.",
        ("Diarrhea or constipation", "Stomach pain or heartburn", "Less appetite", "Back, bone, joint, or muscle aches",
         "Trouble sleeping", "Hot flashes"),
        ("Fever, chills, sore throat, or pain when urinating", "Nosebleeds or unusual bruising or bleeding",
         "Extreme tiredness or pale skin", "Fast heartbeat", "Shortness of breath, cough, or swelling of the legs"),
        MED + "a699019.html",
        tips=("The team checks heart function with scans while on it.",),
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "pertuzumab", "Pertuzumab", ("Perjeta", "Phesgo (with trastuzumab)"), "infusion",
        "Targeted therapy (HER2 antibody)",
        "Treats HER2-positive breast cancer together with trastuzumab and chemotherapy. The \"P\" in TCHP.",
        "Given through the IV, or under the skin when combined with trastuzumab (Phesgo).",
        ("Nausea or diarrhea", "Constipation", "Less appetite", "Trouble sleeping", "Tingling or numbness in the hands or feet",
         "Watery eyes or dry skin"),
        ("Fever, chills, sore throat, cough, or painful urination", "Extreme tiredness or pale skin", "Rash with blisters on the hands or feet",
         "Shortness of breath or swelling of the legs"),
        MED + "a612027.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "pembrolizumab", "Pembrolizumab", ("Keytruda",), "infusion",
        "Immunotherapy (checkpoint inhibitor)",
        "Helps the immune system fight the cancer. Used for many cancers, including some triple-negative breast cancers.",
        "Given through the IV on a set schedule, often every few weeks.",
        ("Muscle, joint, or bone pain", "Skin color changes or dry skin", "Extreme tiredness", "Nausea or vomiting", "Constipation",
         "Less appetite"),
        ("Rash, blisters, peeling skin, or sores in the mouth", "New or worse cough, shortness of breath, or chest pain",
         "Diarrhea, black or bloody stools, or severe belly pain", "Yellow skin or eyes, dark urine, or pain in the upper right belly",
         "Fever or flu-like symptoms"),
        MED + "a614048.html",
        tips=("Immune side effects can appear months later. Tell any doctor or ER that you have had it.",),
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "ado-trastuzumab", "Ado-trastuzumab emtansine", ("Kadcyla",), "infusion",
        "Targeted therapy (HER2 antibody + chemo)",
        "Treats HER2-positive breast cancer.",
        "Given through the IV, usually every 3 weeks.",
        ("Constipation or diarrhea", "Upset stomach", "Mouth sores or dry mouth", "Taste changes", "Joint or muscle pain", "Headache"),
        ("Pain, redness, swelling, or blisters where the IV went in", "Fever, chills, sore throat, or painful urination",
         "Nosebleeds, unusual bleeding, or black stools", "Tingling, burning, or weakness in the hands or feet"),
        MED + "a613031.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "trastuzumab-deruxtecan", "Trastuzumab deruxtecan", ("Enhertu",), "infusion",
        "Targeted therapy (HER2 antibody + chemo)",
        "Treats some HER2-positive and HER2-low breast cancers.",
        "Given through the IV, usually every 3 weeks.",
        ("Nausea or vomiting", "Constipation or diarrhea", "Mouth or lip sores", "Stomach pain or heartburn", "Less appetite"),
        ("New or worse cough or shortness of breath", "Swelling of the ankles or legs, irregular heartbeat, or fainting",
         "Nosebleeds or unusual bleeding", "Fever, chills, or other signs of infection"),
        MED + "a620006.html",
        tips=("Report any new cough or breathing change right away; the team watches the lungs closely.",),
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "sacituzumab", "Sacituzumab govitecan", ("Trodelvy",), "infusion",
        "Targeted chemotherapy (antibody-drug conjugate)",
        "Treats some triple-negative and hormone-receptor-positive breast cancers.",
        "Given through the IV on set days of each cycle.",
        ("Constipation", "Mouth sores", "Stomach pain or heartburn", "Tiredness", "Less appetite or taste changes", "Hair loss"),
        ("Diarrhea that is severe or won't stop", "Fever or other signs of infection", "Easy bruising or bleeding, or blood in urine or stool",
         "Pale skin or unusual tiredness", "Stomach cramps or a lot of saliva"),
        MED + "a620034.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "oxaliplatin", "Oxaliplatin", ("Eloxatin",), "infusion",
        "Chemotherapy (platinum)",
        "Treats colon, rectal, and some other cancers.",
        "Given through the IV by the infusion team.",
        ("Numbness or tingling in the fingers, toes, mouth, or throat, worse with cold", "Nausea or vomiting",
         "Diarrhea or constipation", "Pain in the hands or feet"),
        ("Stumbling or loss of balance", "Trouble writing or doing up buttons", "Trouble speaking, odd tongue feeling, or jaw tightness",
         "Chest pain or pressure"),
        MED + "a607035.html",
        tips=("Cold makes the tingling worse. Many teams suggest avoiding cold drinks and cold air for a few days after.",),
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "irinotecan", "Irinotecan", ("Camptosar",), "infusion",
        "Chemotherapy (topoisomerase inhibitor)",
        "Treats colon, rectal, and some other cancers.",
        "Given through the IV by the infusion team.",
        ("Nausea or vomiting", "Constipation", "Mouth sores", "Heartburn", "Less appetite or weight loss", "Hair loss"),
        ("Diarrhea, especially if severe or with fever", "Chest pain", "Yellow skin or eyes", "Swollen belly or sudden weight gain", "Rash"),
        MED + "a608043.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "etoposide", "Etoposide", ("Toposar", "Etopophos"), "infusion",
        "Chemotherapy (topoisomerase inhibitor)",
        "Treats several cancers, including some lung and testicular cancers.",
        "Given through the IV (a capsule form also exists).",
        ("Nausea or vomiting", "Mouth and throat sores", "Stomach pain, diarrhea, or constipation", "Less appetite",
         "Pain or burning where the IV went in"),
        ("Rash, hives, itching, or trouble breathing or swallowing", "Fast, irregular, or pounding heartbeat", "Seizures"),
        MED + "a697011.html",
        defaults=CLINIC_IV,
    ),
    MedInfo(
        "vinorelbine", "Vinorelbine", ("Navelbine",), "infusion",
        "Chemotherapy (vinca alkaloid)",
        "Treats lung cancer and is sometimes used for breast cancer.",
        "Given through the IV by the infusion team.",
        ("Nausea or vomiting", "Diarrhea", "Less appetite or weight loss", "Taste changes", "Mouth and throat sores"),
        ("Shortness of breath or cough", "Constipation with stomach pain", "Unusual bleeding or bruising",
         "Hives, rash, swelling, or trouble breathing", "Blistering or peeling skin"),
        MED + "a695013.html",
        defaults=CLINIC_IV,
    ),
    # ------------------------------------------------------------ oral cancer
    MedInfo(
        "capecitabine", "Capecitabine", ("Xeloda",), "oral_cancer",
        "Oral chemotherapy",
        "Treats some breast, colon, rectal, stomach, and pancreatic cancers.",
        "Pills taken twice a day, usually within 30 minutes after a meal, on the days the plan says.",
        ("Stomach pain or upset stomach", "Constipation", "Less appetite or taste changes", "Tiredness or weakness", "Headache", "Rash"),
        ("Diarrhea, especially bloody or with fever", "Red, swollen, painful, blistering, or peeling palms or soles",
         "Mouth sores with nausea or vomiting", "Chest pain, fast heartbeat, or shortness of breath", "Yellow skin or eyes, or dark urine"),
        MED + "a699003.html",
        defaults=Defaults(route="oral", kind="scheduled", days=(1, 14), times=("08:00", "20:00"), label="Twice a day after meals"),
        chips=("Diarrhea", "Sore hands or feet", "Nausea", "Mouth sores", "Tired"),
    ),
    MedInfo(
        "abemaciclib", "Abemaciclib", ("Verzenio",), "oral_cancer",
        "Targeted therapy pill (CDK4/6 inhibitor)",
        "Used with hormone therapy for some hormone-receptor-positive breast cancers.",
        "Tablets usually taken twice a day, every day.",
        ("Diarrhea", "Nausea or vomiting", "Stomach pain", "Mouth sores", "Less appetite", "Hair thinning"),
        ("Diarrhea that doesn't improve with the medicine the team gave you", "Yellow skin or eyes, or pain in the upper right belly",
         "Easy bruising or bleeding", "Pain or swelling in an arm or leg, chest pain, or shortness of breath",
         "Fever or other signs of infection"),
        MED + "a617049.html",
        defaults=Defaults(route="oral", kind="scheduled", days=(1, 0), times=("08:00", "20:00"), label="Twice a day"),
        chips=("Diarrhea", "Nausea", "Tired", "Stomach pain"),
    ),
    MedInfo(
        "tamoxifen", "Tamoxifen", ("Soltamox", "Nolvadex"), "oral_cancer",
        "Hormone therapy pill",
        "Blocks estrogen's effect on breast cancer cells. Usually taken for years after other treatment.",
        "Taken by mouth once a day.",
        ("Hot flashes", "Nausea", "Tiredness", "Dizziness or headache", "Mood changes"),
        ("Vision problems", "Unusual bleeding or bruising, or abnormal vaginal bleeding",
         "Leg pain or swelling, chest pain, or shortness of breath", "Yellow skin or eyes"),
        MED + "a682414.html",
        defaults=Defaults(route="oral", kind="scheduled", days=(1, 0), times=("08:00",), label="Once a day"),
        chips=("Hot flashes", "Tired", "Nausea", "Mood changes"),
    ),
    MedInfo(
        "anastrozole", "Anastrozole", ("Arimidex",), "oral_cancer",
        "Hormone therapy pill (aromatase inhibitor)",
        "Lowers estrogen to treat hormone-receptor-positive breast cancer after menopause.",
        "Taken by mouth once a day.",
        ("Hot flashes or sweating", "Joint or bone aches", "Weakness", "Headache", "Nausea"),
        ("Chest pain", "Fever, chills, sore throat, or other signs of infection", "Blurred vision or vision changes", "Yellow skin or eyes"),
        MED + "a696018.html",
        defaults=Defaults(route="oral", kind="scheduled", days=(1, 0), times=("08:00",), label="Once a day"),
        chips=("Hot flashes", "Joint aches", "Tired"),
    ),
    MedInfo(
        "letrozole", "Letrozole", ("Femara",), "oral_cancer",
        "Hormone therapy pill (aromatase inhibitor)",
        "Lowers estrogen to treat hormone-receptor-positive breast cancer after menopause.",
        "Taken by mouth once a day.",
        ("Hot flashes or night sweats", "Joint or bone aches", "Nausea", "Constipation or diarrhea", "Heartburn"),
        ("Chest pain", "Rash, hives, or itching", "Trouble breathing", "Unusual bleeding or bruising"),
        MED + "a698004.html",
        defaults=Defaults(route="oral", kind="scheduled", days=(1, 0), times=("08:00",), label="Once a day"),
        chips=("Hot flashes", "Joint aches", "Tired"),
    ),
    # ------------------------------------------------------------ nausea
    MedInfo(
        "ondansetron", "Ondansetron", ("Zofran",), "nausea",
        "Anti-nausea pill (5-HT3 blocker)",
        "Prevents or eases nausea and vomiting from chemotherapy by blocking serotonin.",
        "Take only as often as the label allows. Dissolving tablet: dry hands, peel the foil back, and place it on the tongue.",
        ("Headache", "Constipation", "Tiredness or drowsiness", "Chills"),
        ("Chest pain, fainting, or a fast, slow, or irregular heartbeat", "Blurred vision or vision loss",
         "Rash, hives, swelling, hoarseness, or trouble breathing", "Agitation, confusion, fever, heavy sweating, or stiff or twitching muscles"),
        MED + "a601209.html",
        tips=("Same family as palonosetron (Aloxi), so it may start a few days after chemo.",),
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 7), label="As needed for nausea"),
        chips=("Headache", "Constipation", "Tired or drowsy"),
    ),
    MedInfo(
        "granisetron", "Granisetron", ("Kytril", "Sancuso patch", "Sustol"), "nausea",
        "Anti-nausea medicine (5-HT3 blocker)",
        "Prevents nausea and vomiting from chemotherapy and radiation.",
        "Comes as a pill, a skin patch, or an injection. Follow the label for which one.",
        ("Headache", "Stomach pain or heartburn", "Constipation", "Trouble sleeping"),
        ("Rash, hives, or trouble breathing or swallowing", "Dizziness, fainting, or a fast, slow, or irregular heartbeat",
         "Agitation, confusion, fever, sweating, or stiff or twitching muscles"),
        MED + "a601211.html",
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 7), label="As needed for nausea"),
        chips=("Headache", "Constipation", "Trouble sleeping"),
    ),
    MedInfo(
        "palonosetron", "Palonosetron", ("Aloxi",), "nausea",
        "Anti-nausea medicine (5-HT3 blocker), given by IV",
        "Prevents nausea and vomiting from chemotherapy by blocking serotonin, a natural substance that can trigger nausea.",
        "Given once through the IV before chemo. Nothing to take at home.",
        ("Headache", "Constipation", "Pain, redness, or swelling where the IV went in"),
        ("Rash, itching, hives, swelling of the face, or trouble breathing or swallowing",
         "Chest pain, dizziness, fainting, or a fast, slow, or irregular heartbeat", "Agitation, confusion, stiff or twitching muscles"),
        MED + "a610002.html",
        tips=("Ondansetron (Zofran) works the same way, so follow the clinic's sheet for the days it can be used.",),
        defaults=Defaults(route="iv", kind="clinic", days=(1, 1), label="Before chemo"),
    ),
    MedInfo(
        "aprepitant", "Aprepitant / fosaprepitant (IV)", ("Cinvanti", "Emend IV"), "nausea",
        "Anti-nausea medicine (NK1 blocker), given by IV",
        "Used with the other anti-nausea medicines to prevent nausea in the first 24 hours and the several days after chemo. It does not treat nausea that has already started.",
        "Usually one IV dose on Day 1, finishing about 30 minutes before chemo.",
        ("Tiredness or weakness", "Diarrhea", "Headache", "Heartburn", "Tingling, numbness, or pain in the arms or legs",
         "Pain or redness where the IV went in"),
        ("Peeling or blistering skin", "Frequent or painful urination, or a sudden urgent need to urinate"),
        MED + "a617042.html",
        defaults=Defaults(route="iv", kind="clinic", days=(1, 1), label="Before chemo"),
    ),
    MedInfo(
        "aprepitant-oral", "Aprepitant (capsules)", ("Emend",), "nausea",
        "Anti-nausea capsule (NK1 blocker)",
        "Taken with other anti-nausea medicines to prevent nausea and vomiting after chemotherapy.",
        "Often one capsule on Day 1 before chemo, then one each morning on Days 2 and 3. Follow the label.",
        ("Weakness or tiredness", "Dizziness", "Diarrhea or constipation", "Gas, stomach pain, or heartburn"),
        ("Hives, rash, or itching", "Peeling or blistering skin", "Trouble breathing or swallowing"),
        MED + "a604003.html",
        defaults=Defaults(route="oral", kind="scheduled", days=(1, 3), times=("08:00",), label="Once a day"),
        chips=("Tired", "Dizzy", "Heartburn"),
    ),
    MedInfo(
        "netupitant-palonosetron", "Netupitant + palonosetron", ("Akynzeo",), "nausea",
        "Combination anti-nausea capsule",
        "Two anti-nausea medicines in one capsule to prevent nausea and vomiting from chemotherapy.",
        "Usually one capsule about an hour before chemo on Day 1 (an IV form also exists).",
        ("Headache", "Stomach pain or heartburn", "Constipation", "Weakness", "Skin redness"),
        ("Hives, rash, or itching", "Trouble breathing or swallowing", "Dizziness, light-headedness, or fainting"),
        MED + "a614053.html",
        defaults=Defaults(route="oral", kind="clinic", days=(1, 1), label="Before chemo"),
    ),
    MedInfo(
        "dexamethasone", "Dexamethasone", ("Decadron",), "nausea",
        "Steroid (corticosteroid)",
        "Given together with the anti-nausea medicines to help prevent nausea after chemo. Also used before some infusions to prevent reactions.",
        "Take with food or milk, because it can upset the stomach. It can make sleep difficult, so ask the team about taking it earlier in the day.",
        ("Trouble sleeping", "Restlessness, anxiety, or mood changes", "Upset or irritated stomach", "Headache or dizziness",
         "Higher blood sugar (check more often if diabetic)"),
        ("Skin rash", "Swollen face, lower legs, or ankles", "Vision problems", "A cold or infection that lasts a long time",
         "Muscle weakness, or black or tarry stool"),
        MED + "a682792.html",
        tips=("If a dose is missed, ask the team what to do and write the answer down.",),
        defaults=Defaults(route="oral", kind="scheduled", days=(2, 4), times=("08:00",), label="Once a day with food"),
        chips=("Trouble sleeping", "Restless or wired", "Upset stomach", "Mood changes", "Headache"),
    ),
    MedInfo(
        "olanzapine", "Olanzapine", ("Zyprexa",), "nausea",
        "Calming medicine used at low dose for nausea",
        "Mainly used for other conditions, but also prescribed to prevent chemotherapy nausea and vomiting.",
        "Usually taken at bedtime on the scheduled days because it causes sleepiness.",
        ("Drowsiness", "Dizziness or light-headedness when standing up", "Dry mouth", "Constipation", "Bigger appetite", "Restlessness"),
        ("Movements of the face or body that can't be controlled", "Fever with stiff muscles, sweating, confusion, or a fast or irregular heartbeat",
         "Seizures or changes in vision", "Trouble with coordination or falls", "Rash with fever, hives, or trouble breathing"),
        MED + "a601213.html",
        tips=("Get out of bed slowly. Sit with feet on the floor for a few minutes before standing.",
              "Alcohol adds to the drowsiness. MedlinePlus says not to drink while taking it.",
              "Don't drive until you know how it affects you."),
        defaults=Defaults(route="oral", kind="scheduled", days=(1, 4), times=("21:00",), label="At bedtime"),
        chips=("Very drowsy next day", "Dizzy when standing", "Dry mouth", "Constipation", "Restless"),
    ),
    MedInfo(
        "prochlorperazine", "Prochlorperazine", ("Compazine",), "nausea",
        "Rescue anti-nausea pill",
        "For nausea or vomiting that breaks through the scheduled medicines.",
        "Take only when needed and no more often than the label allows.",
        ("Drowsiness", "Dizziness or feeling unsteady", "Blurred vision", "Dry mouth or stuffy nose", "Constipation", "Jitteriness or restlessness"),
        ("Fever with muscle stiffness, sweating, confusion, or a fast or irregular heartbeat",
         "Uncontrollable movements of the face, mouth, jaw, or tongue", "Neck cramps, throat tightness, or trouble breathing or swallowing",
         "Yellow skin or eyes", "Rash, hives, or swelling"),
        MED + "a682116.html",
        tips=("It can cause dizziness when getting up. Rise slowly.",),
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 7), label="As needed for nausea"),
        chips=("Drowsy", "Dizzy or unsteady", "Restless or jittery", "Blurred vision", "Dry mouth"),
    ),
    MedInfo(
        "metoclopramide", "Metoclopramide", ("Reglan",), "nausea",
        "Anti-nausea and stomach-emptying pill",
        "Sometimes used for nausea that breaks through other medicines. Also used for reflux and slow stomach emptying.",
        "Take as the label directs.",
        ("Tiredness or weakness", "Restlessness", "Trouble sleeping", "Needing to urinate often"),
        ("Muscle tightening in the jaw or neck, or speech problems", "Uncontrollable movements of the face or body",
         "Fever with muscle stiffness, confusion, sweating, or heartbeat changes", "Depression or thoughts of self-harm",
         "Pacing, foot tapping, or feeling unable to sit still"),
        MED + "a684035.html",
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 7), label="As needed for nausea"),
        chips=("Drowsy", "Restless", "Trouble sleeping"),
    ),
    MedInfo(
        "lorazepam", "Lorazepam", ("Ativan",), "nausea",
        "Calming medicine (benzodiazepine)",
        "Relieves anxiety and trouble sleeping. Some teams use it for nausea that starts before or around treatment.",
        "Take as the label directs. It adds to the sleepiness of other medicines.",
        ("Drowsiness", "Dizziness", "Weakness", "Unsteadiness"),
        ("Rash, hives, swelling of the face, or wheezing", "Yellow skin or eyes", "Very slow or shallow breathing"),
        MED + "a682053.html",
        tips=("Don't drink alcohol or drive while taking it, and ask before mixing with other sleepy medicines.",),
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 7), label="As needed"),
        chips=("Drowsy", "Dizzy", "Unsteady"),
    ),
    MedInfo(
        "promethazine", "Promethazine", ("Phenergan",), "nausea",
        "Antihistamine used for nausea",
        "Used for nausea and vomiting, allergies, and motion sickness.",
        "Comes as a pill, liquid, or suppository. Take as the label directs.",
        ("Drowsiness", "Dry mouth", "Dizziness", "Blurred vision", "Trouble sleeping or nightmares"),
        ("Wheezing or slowed breathing", "Fever with sweating and stiff muscles", "Uncontrollable movements", "Confusion"),
        MED + "a682284.html",
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 7), label="As needed for nausea"),
        chips=("Drowsy", "Dry mouth", "Dizzy"),
    ),
    # ------------------------------------------------------------ blood
    MedInfo(
        "filgrastim", "Filgrastim", ("Neupogen", "Zarxio", "Nivestym", "Releuko", "Nypozi", "Granix"), "blood",
        "White-blood-cell booster (daily shot)",
        "Helps the body make more neutrophils, a kind of white blood cell, to lower the risk of infection during chemotherapy.",
        "Injected under the skin the way the nurse showed you. Keep it in the refrigerator. Most brands can sit at room temperature for up to 24 hours, out of direct sunlight.",
        ("Bone, joint, back, or muscle aches", "Redness, swelling, bruising, or a lump where it was injected", "Headache", "Tiredness",
         "Nausea or constipation"),
        ("Pain in the upper left belly or the tip of the left shoulder", "Fever, shortness of breath, or fast or difficult breathing",
         "Rash, hives, wheezing, or swelling of the face, eyes, or mouth", "Unusual bleeding or bruising", "Peeing less, or dark or bloody urine"),
        MED + "a692033.html",
        defaults=Defaults(route="injection", kind="scheduled", days=(2, 8), times=("15:00",), label="Shot under the skin"),
        chips=("Bone or back aches", "Injection site redness", "Headache", "Tired"),
    ),
    MedInfo(
        "pegfilgrastim", "Pegfilgrastim", ("Neulasta", "Neulasta Onpro", "Fulphila", "Udenyca", "Ziextenzo", "Nyvepria"), "blood",
        "White-blood-cell booster (once per cycle)",
        "A long-acting version of filgrastim. One dose per cycle lowers the risk of infection.",
        "A shot under the skin, usually the day after chemo, or an on-body injector put on at the clinic that gives the dose later on its own.",
        ("Bone pain", "Pain in the arms or legs"),
        ("Pain in the upper left belly or the tip of the left shoulder", "Fever, shortness of breath, or fast or difficult breathing",
         "Swelling of the face, throat, or eyes, hives, or rash", "Swelling of the face or ankles, dark urine, or peeing less",
         "Unusual bleeding or bruising"),
        MED + "a607058.html",
        tips=("With the on-body injector, note when the light shows the dose is done and log it here.",),
        defaults=Defaults(route="injection", kind="scheduled", days=(2, 2), times=("15:00",), label="Once this cycle"),
        chips=("Bone aches", "Arm or leg pain"),
    ),
    # ------------------------------------------------------------ premeds
    MedInfo(
        "diphenhydramine", "Diphenhydramine", ("Benadryl",), "premed",
        "Antihistamine",
        "Given before some infusions (like paclitaxel) to help prevent allergic reactions. Also treats allergy symptoms.",
        "Usually given through the IV or as a pill before the infusion.",
        ("Drowsiness", "Dry mouth, nose, and throat", "Dizziness", "Muscle weakness"),
        ("Vision problems", "Trouble or pain when urinating"),
        MED + "a682539.html",
        defaults=Defaults(route="iv", kind="clinic", days=(1, 1), label="Before the infusion"),
        chips=("Drowsy", "Dry mouth", "Dizzy"),
    ),
    MedInfo(
        "famotidine", "Famotidine", ("Pepcid",), "premed",
        "Stomach acid reducer (H2 blocker)",
        "Given before some infusions to help prevent reactions, and used for heartburn and reflux.",
        "Usually given through the IV or as a pill before the infusion.",
        ("Headache", "Dizziness", "Constipation or diarrhea"),
        ("Hives, rash, or itching", "Swelling of the face, throat, or tongue", "Hoarseness or trouble breathing or swallowing"),
        MED + "a687011.html",
        defaults=Defaults(route="iv", kind="clinic", days=(1, 1), label="Before the infusion"),
        chips=("Headache", "Dizzy"),
    ),
    # ------------------------------------------------------------ comfort
    MedInfo(
        "loperamide", "Loperamide", ("Imodium",), "comfort",
        "Anti-diarrhea medicine",
        "Slows diarrhea. Some cancer medicines come with a specific loperamide plan.",
        "Follow the care team's diarrhea instructions; they can differ from the box.",
        ("Constipation", "Tiredness"),
        ("Diarrhea that doesn't improve, or diarrhea with fever", "Rash, peeling or blistering skin, or hives", "Wheezing or trouble breathing"),
        MED + "a682280.html",
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 0), label="As needed for diarrhea"),
        chips=("Constipation", "Tired"),
    ),
    MedInfo(
        "senna", "Senna", ("Senokot", "Ex-Lax"), "comfort",
        "Stimulant laxative",
        "Short-term relief of constipation, which anti-nausea medicines often cause.",
        "Often taken at bedtime. Follow the label or the team's bowel plan.",
        ("Stomach cramps or discomfort", "Nausea", "Brown-colored urine", "Feeling faint"),
        ("Rectal bleeding", "No bowel movement for several days despite the plan"),
        MED + "a601112.html",
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 0), label="As needed for constipation"),
        chips=("Cramps", "Nausea"),
    ),
    MedInfo(
        "docusate", "Docusate (stool softener)", ("Colace",), "comfort",
        "Stool softener",
        "Softens stool so it is easier to pass, often paired with senna.",
        "Take with a full glass of water as the label directs.",
        ("Stomach or intestinal cramps", "Nausea", "Throat irritation (liquid form)"),
        ("Rash, hives, or trouble breathing", "Fever, vomiting, or stomach pain"),
        MED + "a601113.html",
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 0), label="As needed for constipation"),
        chips=("Cramps", "Nausea"),
    ),
    MedInfo(
        "polyethylene-glycol", "Polyethylene glycol 3350", ("MiraLax",), "comfort",
        "Osmotic laxative (powder)",
        "Treats occasional constipation by keeping water in the stool.",
        "Mix the powder in a drink. Follow the label or the team's bowel plan.",
        ("Bloating or gas", "Cramping", "Nausea"),
        ("Diarrhea", "Hives"),
        MED + "a603032.html",
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 0), label="As needed for constipation"),
        chips=("Bloating", "Cramping"),
    ),
    MedInfo(
        "acetaminophen", "Acetaminophen", ("Tylenol",), "comfort",
        "Pain and fever reliever",
        "Relieves mild to moderate pain and lowers fever.",
        "Take as the label directs and keep track of all products that contain it.",
        ("Usually well tolerated at label doses",),
        ("Red, peeling, or blistering skin, rash, or hives", "Swelling of the face, throat, or tongue, or trouble breathing",
         "Yellow skin or eyes, dark urine, pale stools, or upper belly pain"),
        MED + "a681004.html",
        tips=("During chemo, check the temperature before taking it. It can hide a fever, and a fever needs a call to the team.",),
        defaults=Defaults(route="oral", kind="as_needed", days=(1, 0), label="As needed for pain"),
    ),
    MedInfo(
        "loratadine", "Loratadine", ("Claritin",), "comfort",
        "Antihistamine",
        "Treats allergy symptoms. Some teams suggest it for bone aches from filgrastim or pegfilgrastim.",
        "Usually once a day. Use for bone aches only if the team suggested it.",
        ("Headache", "Drowsiness", "Tiredness"),
        ("Rash, hives, itching, swelling, or trouble breathing or swallowing",),
        MED + "a697038.html",
        defaults=Defaults(route="oral", kind="scheduled", days=(1, 7), times=("08:00",), label="Once a day"),
        chips=("Headache", "Drowsy"),
    ),
    MedInfo(
        "lidocaine-prilocaine", "Lidocaine + prilocaine cream", ("EMLA",), "comfort",
        "Numbing cream",
        "Numbs the skin over a port or IV site before it is accessed.",
        "Apply and cover it ahead of the appointment, exactly as the team or label directs.",
        ("Redness or swelling where applied",),
        ("Hives or swelling of the lips, tongue, face, or throat", "Trouble breathing, dizziness, or fainting", "Pale, gray, or blue skin"),
        MED + "a625032.html",
        defaults=Defaults(route="topical", kind="scheduled", days=(1, 1), times=("07:00",), label="Before port access"),
    ),
]

LIBRARY: dict[str, MedInfo] = {m.key: m for m in _ENTRIES}

GENERIC_CHIPS = ("Drowsy", "Dizzy", "Headache", "Upset stomach", "Constipation")

# Shortcuts in setup. They only tick boxes; schedules and doses are entered per person.
REGIMEN_SHORTCUTS: list[tuple[str, tuple[str, ...]]] = [
    ("AC-T", ("doxorubicin", "cyclophosphamide", "paclitaxel")),
    ("TC", ("docetaxel", "cyclophosphamide")),
    ("TCHP", ("docetaxel", "carboplatin", "trastuzumab", "pertuzumab")),
    ("CMF", ("cyclophosphamide", "methotrexate", "fluorouracil")),
    ("Carboplatin + paclitaxel", ("carboplatin", "paclitaxel")),
]


def lookup(key: str | None) -> MedInfo | None:
    return LIBRARY.get((key or "").strip().lower())


def by_category() -> list[tuple[str, str, str, list[MedInfo]]]:
    return [
        (key, name, blurb, sorted((m for m in _ENTRIES if m.category == key), key=lambda m: m.generic))
        for key, name, blurb in CATEGORIES
    ]


def chips_for(key: str | None) -> tuple[str, ...]:
    info = lookup(key)
    return info.chips if info and info.chips else GENERIC_CHIPS
