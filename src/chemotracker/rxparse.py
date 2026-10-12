"""Read prescription directions that people type or paste in.

Turns text like

    Ondansetron HCl 8MG TAB NORT - Take one tablet by mouth every eight hours
    OLANZapine 5MG TAB - TAKE 1 TABLET BY MOUTH AT BEDTIME. Take on days 2, 3 AND 4 AFTER CHEMO THEN STOP

into medicine fields and cycle-day schedules the rest of the app understands.

How it reads a prescription (see docs/prescription-formats.md for sources):

* A label line is "drug (+ salt) strength form [maker]", for example
  "dexAMETHasone 4MG TAB NORT". Mixed-case "tall man" names, salts (HCl,
  maleate) and maker codes (NORT, TEVA) are ignored when matching.
* Directions ("sig") are split into the parts the NCPDP Structured Sig format
  uses: dose (with ranges like "1-2"), form, route, frequency or interval
  (with ranges like "every 4 to 6 hours"), timing, duration, maximum dose,
  indication ("as needed for nausea"), stop, and "then" steps for tapers.
* Days follow the clinic convention: Day 1 is chemo day, Day 0 the day
  before. "Days 2, 3 and 4", "Days 2 to 8", "the day after chemo",
  "starting the day before chemotherapy for 3 days" all become cycle days.
* Default times follow the Universal Medication Schedule periods (morning,
  noon, evening, bedtime), the same compartments Today uses.

Nothing here decides how to take a medicine. It fills a form the person then
checks against the label, and every guess comes with a note.

Pasted bottle labels also carry Rx numbers, names, addresses and phone
numbers. Those lines are dropped before parsing and the text is never stored.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field

from . import medinfo

# ---------------------------------------------------------------- vocabulary

NUMBER_WORDS = {
    "zero": 0, "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "twenty": 20, "twenty-one": 21, "twenty-four": 24, "twenty-eight": 28, "thirty": 30,
    "half": 0.5, "one-half": 0.5, "one half": 0.5,
}
_NUMWORD = r"(?:one[- ]half|twenty[- ](?:one|four|eight)|zero|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fourteen|fifteen|sixteen|twenty|thirty|half)"
NUM = rf"(?:\d+\s*/\s*\d+|\d+(?:\.\d+)?|{_NUMWORD}\b)"
# A count of days or doses. "a"/"an" never count here: "3 times a day" is not "for 1 day".
NUM_COUNT = rf"(?:\d+(?:\.\d+)?|(?!an?\b){_NUMWORD}\b)"

SALTS = {
    "hcl", "hci", "hydrochloride", "maleate", "mesylate", "sodium", "potassium", "phosphate", "sulfate",
    "citrate", "acetate", "tartrate", "succinate", "besylate", "fumarate", "dihydrate", "monohydrate",
    "anhydrous", "base", "usp", "er", "xr", "sr", "dr", "la", "hbr", "bromide", "calcium", "magnesium",
    "disodium", "dimaleate", "edisylate",
}
FORMS: list[tuple[str, str]] = [
    (r"orally\s+disintegrating(?:\s+tab(?:let)?s?)?|disintegrating\s+tab(?:let)?s?|\bodt\b|tab\s+odt|tbdp|dispersible", "dissolving tablet"),
    (r"\bchew(?:able)?(?:\s+tab(?:let)?s?)?\b", "chewable tablet"),
    (r"\btab(?:let)?s?\b|\btbs?\b|\btb\b", "tablet"),
    (r"\bcap(?:sule)?s?\b|\bcp\b", "capsule"),
    (r"\bsoln?\b|\bsolution\b|\bliquid\b|\bsyrup\b|\belixir\b|\bsusp(?:ension)?\b|\boral\s+conc", "liquid"),
    (r"on[- ]body\s+injector|\bonpro\b|\bpfs\b|pre-?filled\s+syringe|\bsyringe\b|\binj(?:ection)?\b|\bvial\b|\bpen\b|auto-?injector", "injection"),
    (r"\bpatch(?:es)?\b|\btransdermal\b", "patch"),
    (r"\bcream\b|\bointment\b|\boint\b|\bgel\b|\blotion\b", "cream"),
    (r"\bsupp(?:ository|ositories)?\b", "suppository"),
    (r"\bpowder\b|\bpacket\b|\bpwd\b|\bgranules\b", "powder"),
    (r"\blozenge\b|\btroche\b", "lozenge"),
    (r"\bmouthwash\b|\brinse\b", "rinse"),
]
UNIT_WORDS = {
    "tablet": "tablet", "tablets": "tablet", "tab": "tablet", "tabs": "tablet", "pill": "tablet", "pills": "tablet",
    "capsule": "capsule", "capsules": "capsule", "cap": "capsule", "caps": "capsule",
    "ml": "mL", "milliliter": "mL", "milliliters": "mL", "cc": "mL",
    "mg": "mg", "mcg": "mcg", "ug": "mcg", "µg": "mcg", "g": "g", "gm": "g", "gram": "g", "grams": "g",
    "milligram": "mg", "milligrams": "mg", "microgram": "mcg", "micrograms": "mcg",
    "unit": "unit", "units": "unit", "puff": "puff", "puffs": "puff", "spray": "spray", "sprays": "spray",
    "drop": "drop", "drops": "drop", "packet": "packet", "packets": "packet", "capful": "capful", "capfuls": "capful",
    "teaspoon": "teaspoon", "teaspoons": "teaspoon", "tsp": "teaspoon", "tablespoon": "tablespoon",
    "tablespoons": "tablespoon", "tbsp": "tablespoon", "patch": "patch", "patches": "patch",
    "syringe": "syringe", "syringes": "syringe", "injection": "injection", "injections": "injection",
    "dose": "dose", "doses": "dose", "suppository": "suppository", "suppositories": "suppository",
}
PLURAL = {"tablet": "tablets", "capsule": "capsules", "patch": "patches", "suppository": "suppositories",
          "dissolving tablet": "dissolving tablets", "chewable tablet": "chewable tablets"}
VERBS = r"(?:take|inject|apply|dissolve|place|chew|swallow|use|give|insert|mix|instill|inhale|rinse|swish|gargle|put|wear|start|begin|drink)"

# Extra names people write that aren't in the catalog's brand list.
SYNONYMS = {
    "zofran": "ondansetron", "compazine": "prochlorperazine", "decadron": "dexamethasone", "dex": "dexamethasone",
    "zyprexa": "olanzapine", "neupogen": "filgrastim", "zarxio": "filgrastim", "nivestym": "filgrastim",
    "releuko": "filgrastim", "granix": "filgrastim", "tbo-filgrastim": "filgrastim", "neulasta": "pegfilgrastim",
    "onpro": "pegfilgrastim", "udenyca": "pegfilgrastim", "fulphila": "pegfilgrastim", "ziextenzo": "pegfilgrastim",
    "nyvepria": "pegfilgrastim", "fylnetra": "pegfilgrastim", "stimufend": "pegfilgrastim", "emend": "aprepitant-oral",
    "akynzeo": "netupitant-palonosetron", "kytril": "granisetron", "sancuso": "granisetron", "reglan": "metoclopramide",
    "ativan": "lorazepam", "phenergan": "promethazine", "benadryl": "diphenhydramine", "pepcid": "famotidine",
    "imodium": "loperamide", "senokot": "senna", "sennosides": "senna", "colace": "docusate", "miralax": "polyethylene-glycol",
    "peg 3350": "polyethylene-glycol", "tylenol": "acetaminophen", "paracetamol": "acetaminophen", "claritin": "loratadine",
    "emla": "lidocaine-prilocaine", "xeloda": "capecitabine", "verzenio": "abemaciclib", "nolvadex": "tamoxifen",
    "soltamox": "tamoxifen", "arimidex": "anastrozole", "femara": "letrozole", "adriamycin": "doxorubicin",
    "cytoxan": "cyclophosphamide", "taxol": "paclitaxel", "abraxane": "nab-paclitaxel", "taxotere": "docetaxel",
    "herceptin": "trastuzumab", "perjeta": "pertuzumab", "keytruda": "pembrolizumab", "aloxi": "palonosetron",
    "cinvanti": "aprepitant", "fosaprepitant": "aprepitant", "5-fu": "fluorouracil", "5fu": "fluorouracil",
}

ROUTES: list[tuple[str, str]] = [
    (r"\biv\b|\bintravenous(?:ly)?\b|\binfus(?:e|ion|ed)\b|\bivpb\b", "iv"),
    (r"\binject(?:ion)?\b|\bsubcut(?:aneous(?:ly)?)?\b|under the skin|\bsub-?q\b|\bsq\b|\bsc\b|\bim\b|intramuscular", "injection"),
    (r"\bapply\b|\btopical(?:ly)?\b|to (?:the )?skin|port site|\bpatch\b", "topical"),
    (r"by mouth|\borally\b|\bp\.?o\.?\b|\bswallow\b|\bchew\b|on (?:the |your )?tongue|under (?:the |your )?tongue|\bsl\b|sublingual|\bdissolve\b", "oral"),
]
FORM_ROUTE = {"injection": "injection", "patch": "topical", "cream": "topical", "suppository": "other"}

PRN = re.compile(r"\b(?:as needed|prn|p\.r\.n\.?|if needed|when needed|as required|when required|if you need(?: it)?|if necessary|"
                 r"after (?:the first|each|every) (?:loose|watery|unformed) (?:stool|bowel movement)s?|if (?:you have |there is )?(?:no bowel movement|constipat))\b", re.I)
INDICATION_ABBREV = {"n/v": "nausea or vomiting", "n&v": "nausea or vomiting", "n+v": "nausea or vomiting", "nv": "nausea or vomiting",
                     "cinv": "nausea or vomiting", "sob": "shortness of breath", "ha": "headache"}
CHEMO = r"(?:chemo(?:therapy)?|treatment|infusion|your infusion|your chemo(?:therapy)?|your treatment)"
_HNUM = r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty-four)"
# "12 hours and 6 hours before chemo", "12 and 6 hours before chemo": one dose per number.
HOURS_BEFORE = re.compile(rf"\b((?:{_HNUM}\s*(?:hours?|hrs?|h)?\s*(?:,\s*(?:and\s+)?|and\s+|&\s*))*{_HNUM})\s*(?:hours?|hrs?|h)\s+"
                          rf"(?:before|prior to)\s+(?:the start of\s+)?(?:your |each |the )?{CHEMO}\b")
# "x 4 doses", "for 6 doses", "4 doses in total".
DOSE_COUNT = re.compile(rf"(?:\b(?:x|times|for|total of)|×)\s*({NUM_COUNT})\s*(?:total\s+)?doses?\b|\b({NUM_COUNT})\s+doses?\s+(?:in\s+)?(?:total|in all)\b")
# "the day before, the day of and the day after chemo": one schedule over Days 0, 1 and 2.
DAY_LIST = re.compile(rf"\b(?:the\s+)?day\s+(?:before|of|after)(?:\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)(?:the\s+)?day\s+(?:before|of|after))+"
                      rf"\s+(?:your |each )?{CHEMO}\b", re.I)
ANCHOR = r"(?:the\s+)?(?:night|evening|morning|day)\s+(?:before|of|after)\b"
# "2 tablets the night before chemo and 2 tablets the morning of chemo": split before the second anchor.
ANCHOR_AND = re.compile(rf"\s*,?\s*(?:\band\b|&)\s*(?=(?:(?:take|give|use|then)\s+)?(?:{NUM}\s*[a-z]*\s*(?:\([^)]*\)\s*)?)?(?:on\s+|in\s+)?{ANCHOR})", re.I)

SLOT_TIMES = {"morning": "08:00", "breakfast": "08:00", "noon": "12:00", "lunch": "12:00", "afternoon": "14:00",
              "evening": "18:00", "dinner": "18:00", "supper": "18:00", "bedtime": "21:00"}
TIMING = [
    (r"before breakfast|on waking|when you wake", ["07:30"], "morning"),
    (r"\bwith (?:each |every )?meals?\b|\bwith food 3 times|\bac\b(?! ?[a-z])|before meals|after meals|\bpc\b", ["08:00", "12:00", "18:00"], None),
    (r"\b(?:at )?bed ?time\b|\bhs\b|\bqhs\b|\bq\.h\.s\.?\b|\bnightly\b|\bat night\b|before (?:going to )?(?:bed|sleep)|\bevery night\b", ["21:00"], "bedtime"),
    (r"\bevery morning\b|\bin the morning\b|\bqam\b|\bq\.a\.m\.?\b|\bwith breakfast\b|\bmornings?\b", ["08:00"], "morning"),
    (r"\bat noon\b|\bwith lunch\b|\bmidday\b", ["12:00"], "noon"),
    (r"\bevery evening\b|\bin the evening\b|\bqpm\b|\bq\.p\.m\.?\b|\bwith (?:dinner|supper)\b|\bevenings?\b", ["18:00"], "evening"),
]
PER_DAY = [
    (r"\bfour times (?:a|per|each) day\b|\b4 times (?:a|per|each) day\b|\bqid\b|\bq\.i\.d\.?\b|\b4x daily\b|\bfour times daily\b|\b4 times daily\b", 4),
    (r"\bthree times (?:a|per|each) day\b|\b3 times (?:a|per|each) day\b|\btid\b|\bt\.i\.d\.?\b|\bthree times daily\b|\b3 times daily\b|\btds\b|\b3x daily\b", 3),
    (r"\btwice (?:a |per |each )?(?:day|daily)\b|\btwo times (?:a|per|each) day\b|\b2 times (?:a|per|each) day\b|\bbid\b|\bb\.i\.d\.?\b|\btwo times daily\b|\b2 times daily\b|\bbd\b|\b2x daily\b", 2),
    (r"\bonce (?:a |per |each )?(?:day|daily)\b|\bone time (?:a|per|each) day\b|\bdaily\b|\bevery day\b|\beach day\b|\bqd\b|\bq\.d\.?\b|\bq ?day\b|\bq24h?\b|\bonce\s+every\s+day\b", 1),
]
PER_DAY_TIMES = {1: ["08:00"], 2: ["08:00", "20:00"], 3: ["08:00", "14:00", "20:00"], 4: ["08:00", "12:00", "17:00", "21:00"]}
# "8 times a day", "five times daily", "3x a day": any count, read before the fixed patterns above.
PER_DAY_N = re.compile(r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(?:x|times?)\s*"
                       r"(?:(?:a|per|each|every)\s+day|daily)\b")
# A negated clause ("do not take at bedtime") must never become a time, day or frequency.
# "Do not use for more than 3 days", "for no more than 3 days": how many days in all, from the first one.
DAY_LIMIT = re.compile(rf"\b(?:(?:more|longer) than|exceed(?:ing)?)\s+({NUM_COUNT})\s*(?:consecutive\s+|straight\s+)?days?\b")
NEGATION = re.compile(r"\b(?:do not|don'?t|never|avoid|not)\b[^.;,]*", re.I)
TIME_WORDS = re.compile(r"bed ?time|\bnight|\bmorning|\bevening|\bnoon|\bhs\b|\bqhs\b|\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\b", re.I)
DAY_WORDS = re.compile(r"\bon\b[^.;,]*\b(?:days?|chemo(?:therapy)?|treatment|infusion)\b", re.I)
# Words that mean taken on a schedule, never "only if needed".
ATC = re.compile(r"\b(?:a?round[- ]the[- ]clock|atc|on a schedule|regularly|scheduled(?!\s+(?:chemo|treatment|infusion|appointment|visit)))\b", re.I)
# Without a chemo start time on the label, "N hours before chemo" is counted back from this guess (and says so).
CHEMO_START = "09:00"
MASS = {"mcg": 0.001, "mg": 1.0, "g": 1000.0}
COUNT_UNITS = {"tablet", "capsule", "patch", "packet", "suppository", "puff", "spray", "drop", "capful", "syringe", "injection"}

NOISE = [
    r"^\s*(?:rx|rx\s*#|rx\s*no\.?|rx:)\b", r"\bqty\b|\bquantity\b|\bdisp(?:ense)?\b\s*:", r"\brefills?\b|\bno refills\b",
    r"\bndc\b", r"\bdiscard\b|\buse by\b|\bexp(?:ires|iration)?\b\s*:?\s*\d", r"\b(?:orig|fill(?:ed)?|date)\s*:?\s*\d",
    r"\b(?:dr|prescriber|doctor|physician)\b\.?\s*[:A-Z]", r",\s*(?:md|do|np|pa-c|dds|pa)\b\.?$", r"\b(?:md|do|np)\s*$",
    r"warning|caution|federal law|state or federal|transfer of this drug|keep out of reach",
    r"\bpharmacy\b|\bpharmacist\b|\brph\b|\bstore\b\s*#", r"#\s?\d{3,}",
    r"\(\d{3}\)\s*\d{3}[-.\s]\d{4}|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b",
    r"^\s*\d+\s+[\w.'-]+(?:\s+[\w.'-]+){0,4}\s+(?:st|street|ave|avenue|rd|road|blvd|dr|drive|ln|lane|way|pl|place|ct|court|hwy|pkwy|ne|nw|se|sw)\b",
    r"\b[A-Z][a-z]+,?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",
    r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$", r"^[A-Z][A-Za-z'-]+,\s*[A-Z][A-Za-z'-]+(?:\s+[A-Z]\.?)?\s*$",
    r"^\s*(?:by|lot|mfr|mfg|manufacturer)\s*[:#]", r"^\s*(?:pt|patient|name|for)\s*:",
]
NOISE_RE = [re.compile(p, re.I) for p in NOISE]
NAME_LINE = re.compile(r"^[A-Z][A-Za-z'-]+,\s*[A-Z][A-Za-z'-]+(?:\s+[A-Z]\.?)?$")  # LAST, FIRST
STRENGTH = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(mg|mcg|µg|ug|gm|g|ml|units?|iu|%|meq)(?:\s*/\s*(\d+(?:\.\d+)?)?\s*(ml|l|tab|hr|h|actuation|spray|patch|day|24\s*h(?:r|ours?)?)\b)?(?![a-z])",
    re.I,
)


# ---------------------------------------------------------------- results


@dataclass
class ParsedPlan:
    kind: str
    start_day: int
    end_day: int | None
    times: list[str] = field(default_factory=list)
    label: str = ""
    dose: str = ""
    from_label: bool = True

    @property
    def days(self) -> list[int]:
        end = self.end_day if self.end_day is not None else self.start_day
        return list(range(self.start_day, end + 1))


@dataclass
class ParsedMed:
    source: str
    name: str
    info_key: str | None = None
    matched_by: str = ""
    brand: str | None = None
    strength: str | None = None
    form: str | None = None
    route: str = "oral"
    dose: str | None = None
    directions: str | None = None
    min_hours_between: float | None = None
    max_per_day: int | None = None
    plans: list[ParsedPlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cycle_hint: int | None = None
    indication: str | None = None
    prep_minutes: int | None = None
    prep_note: str | None = None
    # The label says there's no step before a dose (on-body injector), so a catalog fridge reminder doesn't apply.
    no_prep: bool = False

    @property
    def kind(self) -> str:
        return self.plans[0].kind if self.plans else "scheduled"

    @property
    def info(self) -> medinfo.MedInfo | None:
        return medinfo.lookup(self.info_key)


# ---------------------------------------------------------------- helpers


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    t = text.strip().lower().replace("–", "-")
    if t in NUMBER_WORDS:
        return float(NUMBER_WORDS[t])
    t = t.replace(" ", "")
    if "/" in t:
        a, b = t.split("/", 1)
        try:
            return float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(t)
    except ValueError:
        return None


def _fmt(n: float) -> str:
    if n == 0.5:
        return "½"
    if n == int(n):
        return str(int(n))
    if n % 1 == 0.5:
        return f"{int(n)}½"
    return f"{n:g}"


def _measure(n: float) -> str:
    return f"{n:g}"


def normalize(text: str) -> str:
    t = text.replace("\r", "\n").replace("\u00a0", " ")
    t = re.sub(r"[‐‑‒–—―]", "-", t)
    t = t.replace("½", " 1/2").replace("¼", " 1/4").replace("¾", " 3/4")
    t = re.sub(r"[“”]", '"', t).replace("’", "'")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)  # "3,000 mg" is 3000 mg
    t = re.sub(r"(?<![\w.])\.(?=\d)", "0.", t)  # a naked decimal point: ".5 mg" is 0.5 mg (ISMP)
    return t


def sentence_case(text: str) -> str:
    """Tidy SHOUTED label text one sentence at a time; leave normal text alone."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(_sentence_case_one(p) for p in parts if p)


def _sentence_case_one(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.6:
        return text.strip()
    out = text.lower()
    out = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)
    out = re.sub(r"\b(am|pm)\b", lambda m: m.group(1).upper(), out)
    out = re.sub(r"\b(iv|prn|po)\b", lambda m: m.group(1).upper(), out)
    return out.strip()


def _clean_name(raw: str) -> str:
    words = [w for w in re.split(r"[\s/]+", raw) if w]
    keep = [w for w in words if w.lower().strip(".,") not in SALTS]
    return " ".join(keep).strip(" -,:;.")


# ---------------------------------------------------------------- drug names

_ALIASES: dict[str, list[str]] | None = None


def aliases() -> dict[str, list[str]]:
    global _ALIASES
    if _ALIASES is None:
        out: dict[str, list[str]] = {}

        def add(alias: str, key: str) -> None:
            alias = re.sub(r"\s+", " ", alias.lower()).strip(" -")
            if len(alias) >= 3 and key not in out.setdefault(alias, []):
                out[alias].append(key)

        for key, info in medinfo.LIBRARY.items():
            add(key.replace("-", " "), key)
            generic = re.sub(r"\(.*?\)", "", info.generic)
            for part in re.split(r"\s*(?:/|\+)\s*", generic):
                add(part, key)
            add(generic, key)
            for brand in info.brands:
                add(re.sub(r"\(.*?\)|\b(?:patch|iv|onpro)\b", "", brand), key)
        for alias, key in SYNONYMS.items():
            add(alias, key)
        out.pop("emend iv", None)
        _ALIASES = out
    return _ALIASES


def match_drug(text: str, route_hint: str = "") -> tuple[str | None, str, str | None]:
    """(catalog key, how it matched, brand typed) for the start of a label line."""
    low = " " + re.sub(r"[^a-z0-9+\- ]", " ", text.lower()) + " "
    spaced = re.sub(r"(?<=[a-z])[-+](?=[a-z])", " ", low)
    best: tuple[int, str] | None = None
    for alias in aliases():
        for hay in (low, spaced):
            idx = hay.find(f" {alias} ")
            if idx >= 0 and (best is None or len(alias) > len(best[1])):
                best = (idx, alias)
    if best is not None:
        keys = aliases()[best[1]]
        key = _pick(keys, route_hint)
        info = medinfo.lookup(key)
        brand = best[1] if info and best[1] not in (info.key.replace("-", " "), info.generic.lower()) and best[1] in (
            b.lower() for b in info.brands) else None
        if brand is None and best[1] in SYNONYMS and best[1] not in ("dex", "fosaprepitant", "sennosides", "5-fu", "5fu"):
            brand = best[1]
        return key, "name", brand.title() if brand else None
    tokens = [t for t in re.findall(r"[a-z][a-z-]{4,}", text.lower()) if t not in SALTS]
    names = [a for a in aliases() if " " not in a]
    for token in tokens[:3]:
        close = difflib.get_close_matches(token, names, n=1, cutoff=0.84)
        if close:
            return _pick(aliases()[close[0]], route_hint), "spelling", None
    return None, "", None


def _pick(keys: list[str], route_hint: str) -> str:
    if len(keys) == 1:
        return keys[0]
    oral = [k for k in keys if medinfo.LIBRARY[k].defaults.route == "oral"]
    other = [k for k in keys if k not in oral]
    if route_hint == "oral" and oral:
        return oral[0]
    return (other or keys)[0]


# ---------------------------------------------------------------- splitting


def _is_noise(line: str) -> bool:
    if re.match(rf"^\s*{VERBS}\b", line, re.I):
        return False
    return any(p.search(line) for p in NOISE_RE)


def _looks_like_directions(text: str) -> bool:
    return bool(re.match(rf"^\s*{VERBS}\b", text, re.I)) or bool(
        re.search(r"\b(?:by mouth|daily|every|q\d+h?|bid|tid|qid|prn|as needed|at bedtime|days? \d|po\b)\b", text, re.I)
    ) and not STRENGTH.match(text.strip())


def _header_split(line: str) -> tuple[str, str]:
    """Split one line into (label part, directions part)."""
    m = re.search(r"\s+[-:]\s+|\s+-(?=[A-Za-z])|:\s+(?=" + VERBS + r"\b)", line, re.I)
    if m:
        left, right = line[: m.start()], line[m.end():]
        verb_first = bool(re.match(rf"\s*{VERBS}\b", right, re.I))
        if STRENGTH.search(left) or match_drug(left)[0] or (verb_first and len(left.split()) <= 6 and not re.match(rf"\s*{VERBS}\b", left, re.I)):
            return left.strip(), right.strip()
    m = re.search(rf"\b{VERBS}\b", line, re.I)
    if m and m.start() > 0:
        left = line[: m.start()]
        if STRENGTH.search(left) or match_drug(left)[0]:
            return left.strip(" -:"), line[m.start():].strip()
    s = STRENGTH.search(line)
    if s:
        rest = line[s.end():]
        form_end = 0
        for pattern, _ in FORMS:
            fm = re.match(r"\s*(?:" + pattern + r")", rest, re.I)
            if fm:
                form_end = fm.end()
                break
        after = rest[form_end:]
        maker = re.match(r"\s*(?:\([^)]*\)|[A-Z]{2,6}\b(?!\s*(?:daily|every|q\d)))", after)
        if maker and not re.match(r"\s*(?:PO|IV|SL|PRN|BID|TID|QID|QHS|QD|HS|SC|SQ|IM)\b", after):
            after = after[maker.end():]
        if _looks_like_directions(after):
            return line[: s.end() + form_end].strip(), after.strip(" -:,")
    return line.strip(), ""


def split_blocks(text: str) -> list[tuple[str, str]]:
    """[(label line, directions)] for each medicine found in the text."""
    lines = [ln.strip(" \t•*·") for ln in normalize(text).split("\n")]
    blocks: list[dict] = []
    current: dict | None = None
    for line in lines:
        if not line or _is_noise(line):
            continue
        head, rest = _header_split(line)
        is_header = (bool(STRENGTH.search(head) or match_drug(head)[0]) or bool(rest)) and not re.match(rf"^\s*{VERBS}\b", head, re.I)
        if is_header:
            if current and current["head"]:
                blocks.append(current)
                current = None
            if current is None:
                current = {"head": "", "dirs": []}
            current["head"] = head
            if rest:
                current["dirs"].append(rest)
        else:
            if current is None:
                current = {"head": "", "dirs": []}
            current["dirs"].append(line)
    if current and (current["head"] or current["dirs"]):
        blocks.append(current)
    out = []
    for b in blocks:
        dirs = " ".join(d.strip() for d in b["dirs"]).strip()
        dirs = re.sub(r"\s+([.,;])", r"\1", dirs)
        out.append((b["head"], dirs))
    return out


# ---------------------------------------------------------------- label line


def parse_label_line(head: str) -> dict:
    rng = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(mg|mcg|g|units?|ml)\b", head, re.I)
    s = STRENGTH.search(head)
    cut = min(x.start() for x in (rng, s) if x) if (rng or s) else None
    name_part = head[:cut] if cut is not None else head
    strength = form = dose_range = None
    value = unit = None
    if rng:
        u = rng.group(3).lower()
        dose_range = f"{_measure(float(rng.group(1)))}–{_measure(float(rng.group(2)))} {'mL' if u == 'ml' else u}"
    elif s:
        raw_value, raw_unit, per_value, per_unit = s.groups()
        unit = {"µg": "mcg", "ug": "mcg", "gm": "g", "units": "unit", "iu": "unit"}.get(raw_unit.lower(), raw_unit.lower())
        unit = "mL" if unit == "ml" else unit
        value = float(raw_value)
        strength = f"{_measure(value)} {unit}" if unit != "%" else f"{_measure(value)}%"
        if per_unit:
            pu = "mL" if per_unit.lower() == "ml" else per_unit.lower()
            strength += f"/{_measure(float(per_value)) + ' ' if per_value else ''}{pu}"
    tail = head[cut:] if cut is not None else ""
    for pattern, name in FORMS:
        if tail and re.search(pattern, tail, re.I):
            form = name
            break
    if cut is not None and form is None:
        # "Zofran ODT 8 mg": the form can come before the strength.
        for pattern, name in FORMS:
            fm = re.search(pattern, name_part, re.I)
            if fm and fm.start() > 0:
                form, name_part = name, name_part[: fm.start()]
                break
    if cut is None:
        trailing = re.search(r"\s+(tabs?|tablets?|caps?|capsules?|odt|soln|inj|syringe)\s*$", name_part, re.I)
        if trailing:
            form = next(n for pat, n in FORMS if re.search(pat, trailing.group(1), re.I))
            name_part = name_part[: trailing.start()]
        else:
            for pattern, name in FORMS:
                if re.search(pattern, name_part, re.I):
                    form = name
                    break
    return {"name": _clean_name(name_part) or _clean_name(head), "strength": strength, "form": form,
            "strength_value": value, "strength_unit": unit, "dose_range": dose_range}


# ---------------------------------------------------------------- directions


def _days_from_list(text: str) -> list[int]:
    out: list[int] = []
    tokens = re.findall(r"\d+|-|to|through|thru|and|or|,|&", text.lower())
    pending_range = False
    for tok in tokens:
        if tok.isdigit():
            n = int(tok)
            if pending_range and out:
                out.extend(range(out[-1] + 1, n + 1))
            else:
                out.append(n)
            pending_range = False
        elif tok in ("-", "to", "through", "thru"):
            pending_range = True
    return sorted(set(d for d in out if 0 <= d <= 90))


def _runs(days: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for d in sorted(days):
        if runs and d == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], d)
        else:
            runs.append((d, d))
    return runs


@dataclass
class _Seg:
    text: str
    clean: str = ""  # lower case, negated clauses removed
    qty: tuple[float, float] | None = None
    qty_unit: str | None = None
    per_day: int | None = None
    interval: tuple[float, float] | None = None
    gap_text: str = ""  # "every 3 days" when the label says it in days
    times: list[str] = field(default_factory=list)
    has_clock: bool = False
    timing_all: list[str] = field(default_factory=list)
    timing_words: list = field(default_factory=list)
    slot: str | None = None
    prn: bool = False
    atc: bool = False
    days: list[int] = field(default_factory=list)
    start: int | None = None
    length: int | None = None
    length_range: bool = False
    once: bool = False
    weekly: bool = False
    every_other: bool = False
    n_doses: int | None = None
    hours_before: list[float] = field(default_factory=list)
    points: list[tuple[int, str, str]] = field(default_factory=list)  # (cycle day, "HH:MM", label) for each dose
    negations: list[str] = field(default_factory=list)
    day_limit: int | None = None  # "do not use for more than 3 days"
    day_limit_text: str = ""  # that clause as written, to quote back


def _clock(text: str) -> list[str]:
    out = []
    for h, m, ap in re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])", text, re.I):
        hour = int(h) % 12 + (12 if ap.lower().startswith("p") else 0)
        out.append(f"{hour:02d}:{int(m or 0):02d}")
    for h, m in re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*(?:a\.?m|p\.?m))", text, re.I):
        out.append(f"{int(h):02d}:{m}")
    return out


def _minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _ampm(hm: str) -> str:
    h, m = (int(x) for x in hm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _join(items) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _span(start: int, end: int | None) -> str:
    if end is None:
        return "every day of the cycle" if start <= 1 else f"Day {start} to the end of the cycle"
    return f"Day {start}" if start == end else f"Days {start}–{end}"


def _points_text(points: list[tuple[int, str, str]]) -> str:
    by_day: dict[int, list[str]] = {}
    for day, hm, _ in points:
        by_day.setdefault(day, []).append(_ampm(hm))
    return ", then ".join(f"{_join(times)} on Day {day}" for day, times in by_day.items())


def _spread_times(n: int) -> list[str]:
    """n doses a day. Up to 4 use the Universal Medication Schedule; more are spread from 8 AM to 10 PM."""
    if n in PER_DAY_TIMES:
        return list(PER_DAY_TIMES[n])
    n = min(n, 24)
    step = 14 * 60 / (n - 1)
    return sorted({_hm(int(round((8 * 60 + i * step) / 15) * 15)) for i in range(n)})


def _parse_segment(text: str) -> _Seg:
    seg = _Seg(text=text)
    raw = " " + text.lower() + " "
    shown = " " + text + " "
    for neg in NEGATION.finditer(raw):
        clause = shown[neg.start():neg.end()] if len(shown) == len(raw) else neg.group(0)
        seg.negations.append(clause.strip(" ,;."))
    t = NEGATION.sub(" ", raw)
    seg.clean = t
    lim = DAY_LIMIT.search(raw)
    if lim and _num(lim.group(1)) and _num(lim.group(1)) == int(_num(lim.group(1)) or 0):
        seg.day_limit = int(_num(lim.group(1)) or 0)
        quoted = next((c for c in seg.negations if DAY_LIMIT.search(c.lower())), None)
        if quoted is None:  # "for no more than 3 days": quote it from "for"
            q = re.search(rf"(?:\bfor\s+)?(?:\bno\s+)?{DAY_LIMIT.pattern}", raw) or lim
            quoted = (shown[q.start():q.end()] if len(shown) == len(raw) else q.group(0)).strip()
        seg.day_limit_text = quoted
    m = re.search(rf"\b{VERBS}\s+({NUM})(?:\s*(?:-|to|or)\s*({NUM}))?\s*([a-zµ]+)?", t)
    if not m:
        m = re.search(rf"(?:^|\s)({NUM})(?:\s*(?:-|to|or)\s*({NUM}))?\s*(tablets?|tabs?|capsules?|caps?|pills?|ml|mg|mcg|g|units?|puffs?|drops?|packets?|patch(?:es)?|syringes?)\b", t)
    if m:
        a, b = _num(m.group(1)), _num(m.group(2)) if m.group(2) else None
        unit = UNIT_WORDS.get((m.group(3) or "").lower())
        if a is not None and (unit or re.match(rf"\s*{VERBS}", m.group(0))):
            seg.qty = (a, b if b is not None else a)
            seg.qty_unit = unit
    iv = re.search(rf"\b(?:every|q\.?)\s*({NUM})\s*(?:(?:-|to|or)\s*({NUM})\s*)?(?:hours?|hrs?|h)\b", t)
    if not iv:
        iv = re.search(r"\bq\s?(\d+)(?:\s?-\s?(\d+))?\s?h(?:r|rs|ours?)?\b", t)
    if iv:
        lo, hi = _num(iv.group(1)), _num(iv.group(2)) if iv.group(2) else None
        if lo:
            seg.interval = (lo, hi or lo)
    elif re.search(r"\bevery hour\b|\bhourly\b", t):
        seg.interval = (1.0, 1.0)
    every_days = re.search(r"\bevery\s+(2|3|4|5|6|two|three|four|five|six)\s+days\b|\bq\s?([2-6])\s?d(?:ays)?\b", t)
    if every_days and not seg.interval:
        n = _num(every_days.group(1) or every_days.group(2))
        if n:
            seg.interval = (n * 24, n * 24)
            seg.gap_text = every_days.group(0).strip()
    count = PER_DAY_N.search(t)
    if count and _num(count.group(1)):
        seg.per_day = int(_num(count.group(1)) or 1)
    else:
        for pattern, n in PER_DAY:
            if re.search(pattern, t):
                seg.per_day = n
                break
    times_hm = _clock(t)
    if times_hm:
        seg.times = times_hm
        seg.has_clock = True
    for pattern, times, slot in TIMING:
        found = re.search(pattern, t)
        if found:
            if not seg.timing_words:  # the first one named sets the time, as before
                if not seg.times:
                    seg.times = list(times)
                seg.slot = slot or seg.slot
            seg.timing_words.append((times[0], found.group(0).strip()))
            seg.timing_all.extend(times)
    seg.timing_all = sorted(set(seg.timing_all))
    seg.timing_words = [word for _, word in sorted(seg.timing_words)]  # in clock order: "in the morning and at bedtime"
    seg.prn = bool(PRN.search(t))
    seg.atc = bool(ATC.search(t))
    seg.weekly = bool(re.search(r"\b(?:weekly|once a week|every week|q ?wk|q ?week)\b", t))
    seg.every_other = bool(re.search(r"\bevery other day\b|\bqod\b|\balternate days\b", t))
    seg.once = bool(re.search(r"\b(?:once|one time|single dose|x ?1 dose|one dose)\b(?! ?(?:a|per|each|daily|every|weekly))", t))

    m = re.search(r"\bdays?\s+(\d+(?:(?:\s*(?:,|\band\b|&|\bor\b|\bto\b|\bthrough\b|\bthru\b|-)\s*)+(?:days?\s+)?\d+)*)", t)
    if m:
        seg.days = _days_from_list(m.group(1))
    listed = DAY_LIST.search(t)
    if listed and not seg.days:
        seg.days = sorted({{"before": 0, "of": 1, "after": 2}[w] for w in re.findall(r"day\s+(before|of|after)", listed.group(0))})
    # Anchors relative to chemo day (Day 1).
    if re.search(rf"\b(?:the )?(?:day|night|evening) before (?:your |each )?{CHEMO}", t):
        seg.start = 0
        if re.search(rf"\b(?:night|evening) before", t) and not seg.times:
            seg.times = ["21:00"] if "night" in t else ["18:00"]
    elif re.search(rf"\b(?:the )?(?:evening|night) of (?:your |each )?{CHEMO}\b|\bafter {CHEMO} (?:that|in the) (?:evening|night)\b", t):
        seg.start = 1
        if not seg.times:
            seg.times = ["18:00"] if "evening" in t else ["21:00"]
    elif re.search(rf"\b(?:the )?(?:day|morning) of (?:your |each )?{CHEMO}\b|\bon {CHEMO} days?\b|\b{CHEMO} day\b|\bbefore (?:your |each )?{CHEMO}\b|\bprior to (?:your |each )?{CHEMO}\b", t):
        seg.start = 1
        if re.search(r"\bmorning of\b", t) and not seg.times:
            seg.times = ["08:00"]
    m = re.search(rf"\b(?:starting|beginning|start)?\s*(?:on )?(?:the )?(day|morning) after (?:your |each )?{CHEMO}\b", t)
    if m and seg.start is None:
        seg.start = 2
        if m.group(1) == "morning" and not seg.times:
            seg.times = ["08:00"]
    m = re.search(rf"\b({NUM})\s*(?:full )?days? (?:after|following) (?:your |each )?{CHEMO}\b", t)
    if m and seg.start is None and not seg.days:
        n = _num(m.group(1))
        if n is not None:
            if re.search(rf"\bfor\s+{re.escape(m.group(1))}\s*(?:full )?days? (?:after|following)", t):
                seg.start, seg.length = 2, int(n)
            else:
                seg.start = 1 + int(n)
    m = re.search(rf"\b({NUM})\s*(?:hours?|hrs?|h) (?:after|following) (?:your |each |the end of )?{CHEMO}\b", t)
    if m and seg.start is None:
        n = _num(m.group(1)) or 24
        seg.start = 1 + max(1, int(n // 24))
    m = re.search(r"\bstart(?:ing)?\s+(?:on\s+)?day\s+(\d+)\b", t)
    if m and seg.start is None:
        seg.start = int(m.group(1))
        seg.days = []
    m = re.search(rf"\b(?:for|x|times|×)\s*({NUM_COUNT})(?:\s*(?:-|to|or)\s*({NUM_COUNT}))?\s*(?:more |total |additional |consecutive )?days?\b", t)
    if not m:
        m = re.search(rf"\b(?:for|x)\s*({NUM_COUNT})(?:\s*(?:-|to)\s*({NUM_COUNT}))?\s*(?:d)\b", t)
    if m:
        lo, hi = _num(m.group(1)), _num(m.group(2)) if m.group(2) else None
        if lo:
            seg.length = int(hi or lo)
            seg.length_range = hi is not None and hi != lo
    dc = DOSE_COUNT.search(t)
    if dc:
        n = _num(dc.group(1) or dc.group(2))
        if n and n >= 2 and n == int(n):
            seg.n_doses = int(n)
    hb = HOURS_BEFORE.search(t)
    if hb:
        offsets = [h for h in (_num(x) for x in re.findall(rf"\b{_HNUM}\b", hb.group(1))) if h]
        if len(offsets) >= 2 or (offsets and offsets[0] >= 2):
            start = _minutes(CHEMO_START)
            points = []
            for h in offsets:
                total = start - int(round(h * 60))
                points.append((1 + total // 1440, _hm(total % 1440), f"{_fmt(h)} {'hour' if h == 1 else 'hours'} before chemo"))
            if all(day >= 0 for day, _, _ in points):
                seg.hours_before = offsets
                seg.points = sorted(points)
    return seg


def _split_then(directions: str) -> list[str]:
    parts = re.split(r"[,;.]?\s*\b(?:and )?then\b\s*(?!stop\b|discontinue\b|as directed\b)", directions, flags=re.I)
    out = []
    for p in parts:
        p = p.strip(" ,;.")
        if not p:
            continue
        if out and not re.search(rf"{VERBS}|\d|daily|twice|once|times|every|bedtime|morning|evening|day|as needed|\bprn\b|if needed", p, re.I):
            out[-1] = out[-1] + " then " + p
        else:
            out.append(p)
    return out or [directions]


def _split_anchors(step: str) -> list[str]:
    """'2 tablets the night before chemo and 2 tablets the morning of chemo' is two doses, one per anchor."""
    if DAY_LIST.search(step):
        return [step]
    pieces = [p.strip(" ,;.") for p in ANCHOR_AND.split(step) if p.strip(" ,;.")]
    if len(pieces) < 2 or not all(re.search(ANCHOR, p, re.I) for p in pieces):
        return [step]
    if re.search(rf"\b{CHEMO}\b", pieces[-1], re.I):  # "the night before and the morning of chemo"
        pieces = [p if re.search(rf"{ANCHOR}\s+(?:your |each )?{CHEMO}", p, re.I) else f"{p} chemo" for p in pieces]
    return pieces


def _split_steps(directions: str) -> list[str]:
    return [piece for step in _split_then(directions) for piece in _split_anchors(step)]


# ---------------------------------------------------------------- whole medicine


def _dose_text(qty: tuple[float, float] | None, unit: str | None, label: dict, form: str | None) -> str | None:
    if not qty:
        return None
    lo, hi = qty
    amount = _fmt(lo) if lo == hi else f"{_fmt(lo)}–{_fmt(hi)}"
    if unit in (None, "tablet", "capsule", "patch", "suppository", "dose", "syringe", "injection", "packet") or unit == (form or ""):
        base = unit if unit not in (None, "dose") else (form or "tablet")
        if base in ("syringe", "injection"):
            base = "injection"
        word = base if (lo == hi == 1 or (hi <= 1)) else PLURAL.get(base, base + "s")
        text = f"{amount} {word}"
        if label.get("strength_value") and label.get("strength_unit") in ("mg", "mcg", "g", "unit") and "/" not in (label.get("strength") or ""):
            total_lo = lo * label["strength_value"]
            total_hi = hi * label["strength_value"]
            mg = _measure(total_lo) if lo == hi else f"{_measure(total_lo)}–{_measure(total_hi)}"
            text += f" ({mg} {label['strength_unit']})"
        return text
    amount = _measure(lo) if lo == hi else f"{_measure(lo)}–{_measure(hi)}"
    text = f"{amount} {unit}"
    if unit == label.get("strength_unit") and label.get("strength_value") and lo == hi and form in ("tablet", "capsule"):
        count = lo / label["strength_value"]
        if count >= 0.5 and abs(count * 2 - round(count * 2)) < 1e-9:
            word = form if count <= 1 else PLURAL.get(form, form + "s")
            text = f"{_fmt(count)} {word} ({text})"
    return text


def _dose_conflict(qty: tuple[float, float] | None, unit: str | None, label: dict) -> str | None:
    """A note when the dose's unit can't be right for the strength ("8 mcg" of an 8 mg tablet). Units are never changed."""
    strength, s_unit = label.get("strength_value"), label.get("strength_unit")
    if not qty or unit not in MASS or not strength or s_unit not in MASS:
        return None
    lo = qty[0] * MASS[unit] / (strength * MASS[s_unit])
    hi = qty[1] * MASS[unit] / (strength * MASS[s_unit])
    if 0.01 <= lo and hi <= 100:
        return None
    amount = _measure(qty[0]) if qty[0] == qty[1] else f"{_measure(qty[0])}–{_measure(qty[1])}"
    return (f"The label strength is {_measure(strength)} {s_unit}, but the dose says {amount} {unit}. "
            "The app left the dose empty. Check the label and fill it in on the medicine page.")


def _per_dose_in(unit: str, qty: tuple[float, float] | None, qty_unit: str | None, label: dict) -> float | None:
    """How much one dose is in the unit a daily limit uses, or None when the label doesn't say exactly."""
    if not qty or qty[0] != qty[1]:
        return None
    n = qty[0]
    dose_unit = None if qty_unit in (None, "dose") else qty_unit
    strength, s_unit = label.get("strength_value"), label.get("strength_unit")
    each = strength if strength and s_unit in MASS and "/" not in (label.get("strength") or "") else None
    if dose_unit == unit or (dose_unit is None and unit in COUNT_UNITS):
        return n
    if unit in MASS:
        if dose_unit in MASS:
            return n * MASS[dose_unit] / MASS[unit]
        if (dose_unit is None or dose_unit in COUNT_UNITS) and each:
            return n * each * MASS[s_unit] / MASS[unit]
        return None
    if unit in COUNT_UNITS and dose_unit in MASS and each:
        units = n * MASS[dose_unit] / (each * MASS[s_unit])
        return units if abs(units * 4 - round(units * 4)) < 1e-9 else None
    return None


def _unit_text(unit: str, amount: float) -> str:
    if unit in COUNT_UNITS and amount != 1:
        return PLURAL.get(unit, unit + "s")
    return unit


def _interval_times(hours: float) -> list[str]:
    n = max(1, min(int(round(24 / hours)), 6))
    start = 8 if hours >= 12 else 6
    return sorted({f"{int((start + i * hours) % 24):02d}:00" for i in range(n)})


def _dose_points(s: _Seg, start_day: int) -> list[tuple[int, str, str]]:
    """Each of 'x N doses', from the first day and time, at the label's interval or times a day."""
    n = s.n_doses or 0
    if s.interval and s.interval[0] <= 24:
        first = _minutes(s.times[0] if s.times else _interval_times(s.interval[0])[0])
        totals = [first + int(round(k * s.interval[0] * 60)) for k in range(n)]
        return [(start_day + total // 1440, _hm(total % 1440), "") for total in totals]
    if s.per_day:
        times = s.times if len(s.times) == s.per_day else _spread_times(s.per_day)
        points: list[tuple[int, str, str]] = []
        day = start_day
        while len(points) < n:
            points.extend((day, hm, "") for hm in times[: n - len(points)])
            day += 1
        return points
    return []


def _plan_label(s: _Seg, kind: str, med: ParsedMed, full: str, times: list[str], onbody: bool) -> str:
    bits = []
    if kind == "as_needed":
        bits.append(f"As needed for {med.indication}" if med.indication else "As needed")
    elif onbody:
        bits.append("On-body injector")
    elif (s.slot == "bedtime" or re.search(r"\bbed ?time\b|\bqhs\b|\bat night\b", s.clean)) and times in ([], ["21:00"]):
        bits.append("At bedtime")
    food = re.search(r"\b(with food|with (?:a )?meals?|with milk|on an empty stomach|with (?:a full )?glass of water|after (?:a )?meals?|before meals)\b", s.clean)
    if food:
        bits.append(food.group(1).capitalize() if not bits else food.group(1))
    before = re.search(rf"\b(\d+|one|two|thirty|30|45|60)\s*(hours?|hrs?|minutes?|mins?)\s+(?:before|prior to)\s+(?:your |each )?{CHEMO}", s.clean)
    if before and kind != "as_needed":
        bits.append(f"{before.group(1)} {before.group(2)} before chemo")
    if med.route == "injection" and kind != "clinic" and not bits:
        bits.append("Shot under the skin" if re.search(r"subcut|under the skin|sub-?q|\bsq\b|\bsc\b", full, re.I) else "Injection")
    return " ".join(bits)[:60]


LIMIT = re.compile(
    rf"(?:(?:do not|don'?t)\s+(?:take|use|give)\s+more\s+than|(?:do not|don'?t)\s+exceed|not\s+to\s+exceed|(?:no|not|never)\s+more\s+than"
    rf"|at\s+most|max(?:imum)?(?:\s+of)?|up\s+to)\s+({NUM})\s*([a-z]+)?\s*(?:in|per|a|each|every|within|/)?\s*"
    rf"(?:24\s*h(?:ours?|rs?)?|day|daily|period of 24)")


def parse_medicine(head: str, directions: str) -> ParsedMed:
    label = parse_label_line(head) if head else {"name": "", "strength": None, "form": None,
                                                    "strength_value": None, "strength_unit": None}
    full = f"{head} {directions}"
    route = ""
    for pattern, r in ROUTES:
        if re.search(pattern, full, re.I):
            route = r
            break
    key, matched_by, brand = match_drug(head or directions, route)
    info = medinfo.lookup(key)
    med = ParsedMed(source=(head + (" - " if head and directions else "") + directions).strip(), name="")
    med.info_key = key
    med.matched_by = matched_by
    med.brand = brand or (info.brands[0] if info and info.brands else None)
    # Pegfilgrastim on-body injector: the clinic puts it on and it gives the dose by itself about 27 hours later.
    onbody = key == "pegfilgrastim" and bool(re.search(r"\bon[- ]?body\b|\bonpro\b", full, re.I))
    if onbody and (brand is None or brand.lower() == "neulasta"):
        med.brand = "Neulasta Onpro"
    med.name = info.generic if info else (label["name"].title() if label["name"] else "")
    if not med.name:
        med.notes.append("Couldn't find the medicine name. Type it in.")
    med.form = label["form"]
    med.strength = f"{label['strength']} {label['form']}".strip() if label["strength"] and label["form"] else label["strength"]
    med.route = route or FORM_ROUTE.get(label["form"] or "", "") or (info.defaults.route if info else "oral")
    med.directions = sentence_case(directions) if directions else None
    if not directions:
        med.notes.append("No directions found. Copy them from the label.")

    m = re.search(r"\b(?:as needed|prn|if needed|when needed|as required)\s+(?:for|to treat|to prevent|with)\s+([a-z ,/&-]+?)(?=$|[.;]| do not| not to| no more| max| up to| maximum| x \d| for \d| every| then| on days?)", " " + directions.lower() + " ")
    if not m:
        m = re.search(r"\bfor\s+(nausea(?: (?:and|or|/) vomiting)?|vomiting|pain|fever|constipation|diarrh?(?:o)?ea|sleep|anxiety|itching|heartburn|loose stools?)\b", directions.lower())
    if m:
        med.indication = re.sub(r"\s+", " ", m.group(1)).strip(" ,/").replace("/", " or ")
    else:
        ab = re.search(r"\b(?:prn|as needed|if needed)\s+(?:for\s+)?(n/v|n&v|n\+v|nv|cinv|sob|ha)\b", directions.lower())
        if ab:
            med.indication = INDICATION_ABBREV[ab.group(1)]
    cyc = re.search(r"(?:of|in) (?:each|every|a|the|your)\s+(\d+)[- ]?day (?:treatment )?cycle|\bevery (\d+) days\b|\bq ?(\d+) ?d(?:ays)?\b|\bq ?([1-4]) ?w(?:ee)?ks?\b", directions.lower())
    if cyc:
        n = next(int(g) for g in cyc.groups() if g)
        hint = n * 7 if cyc.group(4) else n
        if hint >= 7:  # "every 3 days" is how often a dose is given, not a cycle
            med.cycle_hint = hint
    mx = LIMIT.search(directions.lower())

    segs = [_parse_segment(s) for s in _split_steps(directions)] if directions else [_Seg(text="")]
    if re.search(rf"\bclinic\b|\bin (?:the )?(?:infusion|treatment) (?:center|room|suite)\b|\bby (?:your|the) nurse\b|\binfus", full, re.I) or med.route == "iv":
        base_kind = "clinic"
    else:
        base_kind = ""
    first_qty = next((s.qty for s in segs if s.qty), None)
    first_unit = next((s.qty_unit for s in segs if s.qty_unit), None)
    med.dose = _dose_text(first_qty, first_unit, label, label["form"])
    if med.dose is None and label.get("dose_range"):
        med.dose = label["dose_range"]
    elif med.dose is None and label.get("strength") and not label.get("form") and "/" not in label["strength"]:
        # Protocol style ("olanzapine 5 mg PO daily"): the number is the dose, not the tablet size.
        med.dose, med.strength = label["strength"], None
    conflict = _dose_conflict(first_qty, first_unit, label)
    if conflict:
        med.dose = None
        med.notes.append(conflict)

    any_prn = any(s.prn for s in segs)
    any_interval = next((s.interval for s in segs if s.interval), None)
    default_kind = info.defaults.kind if info else ""
    prev_end: int | None = None
    plans: list[ParsedPlan] = []
    used_default_days = False
    limit_explained = False  # an as-needed day limit was quoted, which also says where the days came from
    pointed: set[int] = set()  # plans placed dose by dose from chemo ("12 and 6 hours before chemo")
    qtys = {s.qty for s in segs if s.qty}
    # A step with its own frequency and days that isn't "as needed" is a real schedule ("every 8 hours for 2 days,
    # then every 8 hours as needed"). Keep the steps apart then, or the scheduled part turns into as needed.
    scheduled_step = any(not s.prn and (s.interval or s.per_day or s.times or s.points)
                         and (s.days or s.length or s.start is not None or s.n_doses or s.points) for s in segs)
    if (len(segs) > 1 and not base_kind and (any_prn or default_kind == "as_needed") and not scheduled_step
            and not any(s.per_day or s.times for s in segs[1:])):
        # "2 capsules after the first loose stool, then 1 after each" is one as-needed medicine, not two schedules.
        merged = _parse_segment(directions)
        merged.prn = True
        segs = [merged]
    for s in segs:
        if base_kind:
            kind = "clinic"
        elif s.prn or (any_prn and len(segs) == 1):
            kind = "as_needed"
        elif (s.interval and not s.per_day and not s.times and not s.once and not s.atc and not s.n_doses
              and len(segs) == 1 and default_kind == "as_needed"):
            kind = "as_needed"
            med.notes.append(
                f"Label says every {_fmt(s.interval[0])} hours, not \"as needed\". Set as needed, at least "
                f"{_fmt(s.interval[0])} hours apart. Change it if you were told to take it around the clock.")
        else:
            kind = "scheduled"
        seg_dose = _dose_text(s.qty, s.qty_unit or first_unit, label, label["form"]) if (len(segs) > 1 and s.qty) else ""
        if seg_dose and _dose_conflict(s.qty, s.qty_unit or first_unit, label):
            seg_dose = ""
        # Doses placed one by one: "12 hours and 6 hours before chemo", "every 6 hours x 4 doses".
        points = s.points if kind == "scheduled" else []
        if kind == "scheduled" and not points and s.n_doses:
            start = s.days[0] if s.days else (s.start if s.start is not None else (
                prev_end + 1 if prev_end is not None else (info.defaults.days[0] if info else 1)))
            points = _dose_points(s, start)
            if points and max(day for day, _, _ in points) > 90:  # past what a cycle can hold
                points = []
            if points:
                med.notes.append(f"Label says {s.n_doses} doses. The app set them for {_points_text(points)}. "
                                 "Change the times if you were told others.")
                if not s.days and s.start is None and prev_end is None:
                    med.notes.append(f"Start day isn't on the label, so this starts on {'chemo day (Day 1)' if start == 1 else f'Day {start}'}.")
            else:
                med.notes.append(f"Label says {s.n_doses} doses. The app couldn't tell when, so check the days and times.")
        elif kind != "scheduled" and s.n_doses:
            med.notes.append(f"Label says {s.n_doses} doses in all. The app can't count that for you, so keep track and follow the label.")
        if points:
            by_day: dict[int, list[tuple[str, str]]] = {}
            for day, hm, lbl in points:
                by_day.setdefault(day, []).append((hm, lbl))
            for day, entries in sorted(by_day.items()):
                times = sorted({hm for hm, _ in entries})
                lbl = " and ".join(dict.fromkeys(x for _, x in entries if x)) or _plan_label(s, kind, med, full, times, onbody)
                plans.append(ParsedPlan(kind=kind, start_day=day, end_day=day, times=times, label=lbl[:60],
                                        dose=seg_dose or "", from_label=True))
                pointed.add(len(plans) - 1)
            prev_end = max(by_day)
            if s.hours_before:
                med.notes.append(
                    f"Label says {_join([_fmt(h) for h in s.hours_before])} hours before chemo. The app doesn't know when chemo "
                    f"starts, so it guessed {_ampm(CHEMO_START)} and set {_points_text(points)}. Change the times to fit your chemo time.")
            continue
        # Days. A day limit ("do not use for more than 3 days") counts like "for 3 days" from the first day. For an
        # as-needed medicine the first day is whenever it's first needed, not a cycle day, so its days stay the usual
        # ones and the limit is quoted instead: capping them would call a first dose on Day 4 "not planned".
        from_label = True
        limit = s.day_limit if kind != "as_needed" else None
        length = min(n for n in (s.length, limit) if n) if (s.length or limit) else None
        if s.days:
            day_runs = _runs(s.days)
        elif s.start is not None or length:
            start = s.start if s.start is not None else (prev_end + 1 if prev_end is not None else (info.defaults.days[0] if info else 1))
            if s.once or (length is None and kind != "as_needed" and not s.per_day and not s.interval):
                end: int | None = start
            elif length:
                end = start + length - 1
            elif kind == "as_needed":
                end = info.defaults.days[1] if info and info.defaults.days[1] >= start else None
                from_label = False
            else:
                end = start
            day_runs = [(start, end)]  # type: ignore[list-item]
            if s.start is None and prev_end is None:
                from_label = False
                med.notes.append(f"Start day isn't on the label, so this starts on {'chemo day (Day 1)' if start == 1 else f'Day {start}'}.")
            if s.length_range:
                med.notes.append(f"Label gives a range of days. Using the longer one ({s.length} days).")
        elif prev_end is not None:
            start = prev_end + 1
            from_label = False
            if kind == "as_needed":
                # "..., then every 8 hours as needed": as needed from the day after the step before.
                end = info.defaults.days[1] if info and info.defaults.days[1] >= start else None
                med.notes.append(f"The label doesn't say how long to keep it as needed, so this uses {_span(start, end)}. "
                                 "Match it to your clinic's sheet.")
            else:
                end = start
                med.notes.append(f"The label doesn't say how many days for the part after \"then\", so the app set one day "
                                 f"(Day {start}). Change it to match the label.")
            day_runs = [(start, end)]  # type: ignore[list-item]
        elif info:
            d0, d1 = info.defaults.days
            day_runs = [(d0, None if d1 == 0 else d1)]  # type: ignore[list-item]
            from_label = False
            used_default_days = True
        else:
            day_runs = [(1, None)]  # type: ignore[list-item]
            from_label = False
            used_default_days = True
        # Every 72 hours, every other day, weekly: never a dose every day. Keep the first and say so.
        collapsed = False
        long_gap = bool(s.interval and s.interval[0] > 24)
        if kind == "scheduled" and (long_gap or s.every_other or s.weekly) and any(e is None or e != b for b, e in day_runs):
            first_day = day_runs[0][0]
            day_runs = [(first_day, first_day)]
            collapsed = True
            if long_gap:
                gap = s.gap_text or f"every {_fmt(s.interval[0])} hours"
                med.notes.append(f"Label says {gap}. The app can't repeat a dose that far apart, so it "
                                 f"set only the first one, on Day {first_day}. Add the next ones by hand, and follow the label.")
            elif s.every_other:
                med.notes.append(f"Label says every other day. The app set only the first one, on Day {first_day}. "
                                 "Add the other days by hand.")
            else:
                med.notes.append(f"Label says weekly. The app set only Day {first_day}. Set the cycle to 7 days, or pick the day it's given.")
        if s.day_limit:
            said = s.day_limit_text[:1].upper() + s.day_limit_text[1:]
            last = day_runs[-1][1]
            if kind == "as_needed" and not s.days and not s.length:
                med.notes.append(f"The label says \"{said}\". That counts from the first dose, not from chemo day, so "
                                 f"the app kept the usual days ({_span(day_runs[0][0], last)}). Follow the label.")
                limit_explained = True
            elif not s.days and not collapsed and last is not None and length == s.day_limit:
                med.notes.append(f"The label says \"{said}\", so the last day is Day {last}.")
            else:
                med.notes.append(f"The label says \"{said}\". Check that the days follow it.")
        # Times
        times: list[str] = []
        named = ""
        if kind == "scheduled":
            if s.per_day and not s.has_clock:
                if s.times and len(s.times) == s.per_day:
                    times = list(s.times)
                elif s.timing_all and len(s.timing_all) == s.per_day:  # "4 times a day before meals and at bedtime"
                    times = list(s.timing_all)
                    named = _join(s.timing_words) if len(s.timing_words) > 1 else ""
                else:
                    times = _spread_times(s.per_day)
                    if s.per_day == 1 and info and info.defaults.times and info.defaults.kind == "scheduled":
                        times = list(info.defaults.times)[:1]
                    if s.per_day > 4:
                        med.notes.append(f"Label says {s.per_day} times a day. The app spread them from 8:00 AM to 10:00 PM. "
                                         "Change the times if you were told when to take them.")
                    elif s.timing_words:
                        count = {1: "once", 2: "twice"}.get(s.per_day, f"{s.per_day} times")
                        med.notes.append(f"The label says {count} a day and also \"{_join(s.timing_words)}\". The app set "
                                         f"{_join(_ampm(x) for x in times)}. Change the times to match the label.")
            elif s.times:
                times = list(s.times)
            elif s.interval:
                times = _interval_times(s.interval[0])
            elif info and info.defaults.times:
                times = list(info.defaults.times)
            else:
                times = ["08:00"]
        lbl = (named[:1].upper() + named[1:])[:60] if named else _plan_label(s, kind, med, full, times, onbody)
        for start, end in day_runs:
            plans.append(ParsedPlan(kind=kind, start_day=start, end_day=end, times=times,
                                    label=lbl, dose=seg_dose or "", from_label=from_label))
        last_end = day_runs[-1][1]
        prev_end = last_end if last_end is not None else prev_end
        if s.weekly and not collapsed:
            med.notes.append("Label says weekly. Set the cycle to 7 days, or pick the day it's given.")
        if s.every_other and not collapsed:
            med.notes.append("Label says every other day. Pick the days it's taken.")
    if used_default_days and plans and not limit_explained:
        p = plans[0]
        med.notes.append(f"Days aren't on the label, so this uses {_span(p.start_day, p.end_day)}. Match it to your clinic's sheet.")
    for s in segs:
        for clause in s.negations:
            if clause == s.day_limit_text:
                continue  # read as the day limit above
            said = clause[:1].upper() + clause[1:]
            if DAY_WORDS.search(clause) and plans:  # "not on chemo day"
                med.notes.append(f"The label says \"{said}\". Check that the days follow it.")
            elif TIME_WORDS.search(clause) and any(p.kind == "scheduled" for p in plans):  # "do not take at bedtime"
                med.notes.append(f"The label says \"{said}\". Check that the times follow it.")
    # Gap and limits for as-needed medicines.
    if any_interval and any(p.kind == "as_needed" for p in plans):
        med.min_hours_between = any_interval[0]
        if any_interval[1] != any_interval[0]:
            med.notes.append(f"Label says every {_fmt(any_interval[0])} to {_fmt(any_interval[1])} hours. "
                             f"The next dose shows as OK after {_fmt(any_interval[0])}.")
    if mx:
        limit = _num(mx.group(1))
        unit = UNIT_WORDS.get((mx.group(2) or "").lower(), "")
        if limit:
            fixed = len(qtys) <= 1 and (not first_qty or first_qty[0] == first_qty[1])
            if unit in ("", "dose"):
                med.max_per_day = int(limit)
            else:
                # A limit in mg (or tablets) becomes a number of doses only when one dose is known in the same unit.
                per = _per_dose_in(unit, first_qty, first_unit, label) if fixed else None
                if per is None and not first_qty and unit in COUNT_UNITS:
                    per = 1.0
                if per and limit / per >= 1 - 1e-9:
                    med.max_per_day = int(math.floor(limit / per + 1e-9))
                    if not (unit in COUNT_UNITS and per == 1):
                        med.notes.append(f"Label limit: {_fmt(limit)} {_unit_text(unit, limit)} a day. At {_measure(per)} "
                                         f"{_unit_text(unit, per)} per dose that's up to {med.max_per_day} "
                                         f"{'dose' if med.max_per_day == 1 else 'doses'}.")
                else:
                    why = "when doses vary" if not fixed else "and follow the label"
                    med.notes.append(f"Label limit: no more than {_fmt(limit)} {_unit_text(unit, limit)} in 24 hours. The app can't "
                                     f"count that for you, so keep track {why}.")
    # "Remove from refrigerator 30 minutes before use" / "let it sit at room temperature for 30 minutes"
    fridge = re.search(r"(?:out of|from) (?:the )?(?:fridge|refrigerator)\b[^.;]*?(\d{1,3})\s*(?:min|minutes?)\b"
                       r"|room temperature[^.;]*?(\d{1,3})\s*(?:min|minutes?)\b", directions, re.I)
    if onbody:
        # The clinic warms and fills it before putting it on (Neulasta label), so there's nothing to do at home.
        med.no_prep = True
        med.notes.append("On-body injector: the clinic puts it on, and it gives the dose by itself about 27 hours later. "
                         "There's nothing to take out of the fridge, so if the medicine page shows \"Get it ready\", clear it. "
                         "Log the dose when the light shows it's done.")
    elif fridge:
        med.prep_minutes = int(fridge.group(1) or fridge.group(2))
        med.prep_note = "Take it out of the fridge"
        med.notes.append(f"Reminder to take it out of the fridge {med.prep_minutes} minutes before each dose.")
    for i, p in enumerate(plans):
        night = small_hours_note(p.start_day, p.end_day, p.times, after_adding=True, hint=i not in pointed) \
            if p.kind == "scheduled" else None
        if night:
            med.notes.append(night)
            break
    if med.cycle_hint:
        med.notes.append(f"Label mentions a {med.cycle_hint}-day cycle. Check your cycle length matches.")
    if med.matched_by == "spelling":
        med.notes.append(f"Matched by spelling to {med.name}. Check it's right.")
    if not med.info_key and med.name:
        med.notes.append("Not in the built-in list, so it's added as your own medicine.")
    med.notes = list(dict.fromkeys(med.notes))
    med.plans = plans
    return med


def small_hours_note(start_day: int, end_day: int | None, times, after_adding: bool = False,
                     hint: bool = True) -> str | None:
    """Which night a schedule time between midnight and 4:59 AM means, or None.

    A planned dose is dated by its cycle day, so 12:30 AM on Day 2 comes at the start of Day 2: the night between
    Day 1 and Day 2. Premedication like "Day 1 at 3 AM" relies on that. People who think of it as Day 2's bedtime
    mean the night after, so when every time is after midnight the note says which days to pick for that.
    """
    times = [t for t in times if t]
    early = sorted(t for t in times if t < "05:00")
    if not early:
        return None
    s = start_day
    night = f"the night between Day {s - 1} and Day {s}" if s >= 1 else "the night before Day 0"
    out = f"A time after midnight belongs to the start of that day. {_ampm(early[0])} on Day {s} is {night}."
    if hint and len(early) == len(times):
        if end_day is None:
            days = f"start on Day {s + 1}"
        else:
            days = "choose " + (f"Day {s + 1}" if end_day == s else f"Days {s + 1} to {end_day + 1}")
        out += f" If you meant the night after Day {s}, {days}" + (" after adding it." if after_adding else ".")
    return out


def parse(text: str) -> list[ParsedMed]:
    meds = []
    for head, directions in split_blocks(text or ""):
        if not head and not directions:
            continue
        meds.append(parse_medicine(head, directions))
    return meds


def plan_values(plan: ParsedPlan) -> dict:
    """Shape a parsed plan the way store.create_plan expects form values."""
    return {
        "kind": plan.kind,
        "days": [str(d) for d in plan.days] if plan.end_day is not None else [str(plan.start_day)],
        "whole_cycle": "1" if plan.end_day is None else "",
        "times": list(plan.times),
        "label": plan.label,
        "dose": plan.dose,
    }
