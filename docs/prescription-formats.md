# How ChemoTracker reads prescriptions

When you add medicines you can type or paste what's printed on the bottle, or a list like this:

```text
Ondansetron HCl 8MG TAB NORT - Take one tablet by mouth every eight hours
Prochlorperazine Maleate 10 MG TAB NORT - Take one tablet by mouth every six hours
dexAMETHasone 4MG TAB NORT - Take 2 tablets by mouth daily. Take on days 2, 3, AND 4, AFTER CHEMO THEN STOP
OLANZapine 5MG TAB NORT - TAKE 1 TABLET BY MOUTH AT BEDTIME. Take on days 2, 3 AND 4 AFTER CHEMO THEN STOP
```

The app shows what it understood, with real dates counted from chemo day, before anything is saved. The reader lives in `src/chemotracker/rxparse.py`. It is plain pattern matching with no model or download: a 4-line paste takes about 1 ms on a laptop and about 4 ms on a quarter of one CPU core, using under 10 KB of extra memory. That is light enough for a Raspberry Pi.

Photo reading (OCR) was tried and dropped: pill bottles are curved and photos of them read poorly. Typing what's on the bottle is faster and more reliable.

## What a prescription looks like

**The label line** is usually `drug (+ salt) strength form [maker]`:

| Part | Examples | What the app does |
|---|---|---|
| Drug | `OLANZapine`, `dexAMETHasone`, `Zofran` | Matches the built-in list by generic or brand name, ignoring case and small misspellings |
| Salt | `HCl`, `maleate`, `sodium` | Ignored for matching |
| Strength | `8MG`, `10 MG`, `300 mcg/0.5 mL`, `2.5%` | Kept as the strength |
| Form | `TAB`, `CAP`, `ODT`, `SOLN`, `syringe` | Kept with the strength ("8 mg tablet") |
| Maker | `NORT`, `TEVA`, `AURO` | Ignored |

Pharmacies print some names in mixed case, called "tall man" letters (`dexAMETHasone`, `OLANZapine`), to stop look-alike mix-ups. Case doesn't matter to the reader.

**The directions** ("sig") follow the parts of the NCPDP Structured and Codified Sig format:

| Part | Examples |
|---|---|
| Dose | `Take one tablet`, `2 tablets`, `1-2 tablets`, `½ tablet`, `.5 tablet`, `8 mg` |
| Route | `by mouth`, `PO`, `under the skin`, `SUBQ`, `IV`, `on the tongue`, `apply` |
| How often | `daily`, `twice daily`, `BID`, `TID`, `QID`, `3 times a day`, `8 times a day`, `every 8 hours`, `q6h`, `every 4 to 6 hours`, `around the clock` |
| When | `at bedtime`, `QHS`, `in the morning`, `with meals`, `at 9 PM` |
| As needed | `as needed`, `PRN`, `if needed`, `after each loose stool`, plus what for (`for nausea or vomiting`, `n/v`) |
| Limit | `do not exceed 8 tablets in 24 hours`, `no more than 4 a day`, `not more than 3 tablets a day`, `do not exceed 3000 mg per day` |
| Duration | `for 3 days`, `x 3 to 4 days`, `x 4 doses`, `do not use for more than 3 days` |
| Steps | `then` (tapers like `4 mg the evening of chemo, then twice daily for 3 days`), `then stop`, and `and` between chemo-day phrases (`2 tablets the night before and 2 tablets the morning of chemo`) |

## How chemo schedules are written

Clinics number days within each cycle. **Day 1 is chemo day** and the count restarts every cycle. A cycle repeats on a fixed rhythm:

- `every 14 days`, `q14d`, `q2w`, or "dose-dense" for every 2 weeks
- `every 21 days`, `q21d`, or `q3w` for every 3 weeks
- `weekly` or `q7d`

Plans often have parts, for example "AC every 14 days x 4 cycles, followed by paclitaxel weekly x 12". Supportive medicines are tied to cycle days: "filgrastim on Days 2 to 8", "pegfilgrastim on Day 2", "olanzapine on days 2, 3 and 4", "capecitabine on days 1 to 14 of each 21-day cycle".

The reader understands:

| Text | Cycle days |
|---|---|
| `on days 2, 3 and 4`, `days 2 to 8`, `days 1 through 14` | Those days |
| `days 1, 8 and 15` | Three separate days |
| `the day before chemo`, `the night before` | Day 0 |
| `the day of chemo`, `on chemo day`, `1 hour before chemo` | Day 1 |
| `the evening of chemo` | Day 1, 6 PM |
| `the day after chemo`, `starting the day after chemo for 3 days` | Day 2, Days 2–4 |
| `24 hours after chemo` | Day 2 |
| `starting the day before chemotherapy for 3 days` | Days 0–2 |
| `the day before, the day of and the day after chemo` | Days 0, 1 and 2 |
| `the night before chemo and the morning of chemo` | Day 0 at 9 PM and Day 1 at 8 AM |
| `12 and 6 hours before chemo` | Day 0 at 9 PM and Day 1 at 3 AM, counted back from a guessed 9 AM start (with a note) |
| `every 6 hours x 4 doses starting the night before chemo` | 9 PM on Day 0, then 3 AM, 9 AM and 3 PM on Day 1 |

## Default times

When the label gives no clock time, the app uses the Universal Medication Schedule periods, the same Morning, Noon, Evening and Bedtime compartments Today shows:

| Directions | Times |
|---|---|
| once daily, every morning | 8 AM (or the medicine's usual time, e.g. olanzapine at bedtime) |
| twice daily, every 12 hours | 8 AM, 8 PM |
| three times daily | 8 AM, 2 PM, 8 PM |
| four times daily | 8 AM, 12 PM, 5 PM, 9 PM |
| 5 or more times a day | spread from 8 AM to 10 PM (with a note) |
| 4 times a day before meals and at bedtime | 8 AM, 12 PM, 6 PM, 9 PM |
| with meals | 8 AM, 12 PM, 6 PM |
| at bedtime | 9 PM |

## When the label is incomplete

The reader never guesses silently. Each guess shows a note and a "guess" tag:

- **No days on the label** (common for rescue medicines): uses the medicine's usual days from the built-in list and says so.
- **"Every 8 hours" without "as needed"** for a rescue anti-nausea medicine: sets it as needed with an 8-hour gap and says so. These are commonly prescribed that way after chemo. Switch it to "On a schedule" if you were told to take it around the clock. Labels that say `around the clock`, `ATC`, `regularly` or `scheduled`, or that give the doses their own days (`every 8 hours for 2 days, then every 8 hours as needed`), stay on a schedule.
- **"Do not" clauses** (`Do not take at bedtime`, `not on chemo day`) never become a time or a day. The note quotes them so you can check the schedule follows them.
- **A count and a named time that disagree** (`twice daily at bedtime`): keeps the number of doses and says so.
- **Doses more than a day apart** (`every 72 hours`, `every 3 days`, `every other day`, `weekly`): the app can't repeat a dose that way, so it sets only the first one and asks you to add the rest. It never turns them into a dose every day.
- **Chemo start time**: `12 and 6 hours before chemo` is counted back from 9 AM and says so. Change the times to fit when chemo starts.
- **Daily limits in mg** (`do not exceed 3000 mg per day`): counted as doses only when one dose is known in mg, for example 2 tablets of 500 mg is 1000 mg, so up to 3 doses. Otherwise the limit is left empty and the note quotes it.
- **A dose unit that can't match the strength** (`8 mcg` of an 8 mg tablet): the dose is left empty with a note. Units are never changed.
- **Pegfilgrastim on-body injector** (Neulasta Onpro): the clinic puts it on and it gives the dose by itself about 27 hours later. There's nothing to take out of the fridge at home.
- **Ranges** (`every 4 to 6 hours`, `x 3 to 4 days`): uses the shorter gap and the longer number of days, and says so.
- **A day limit** (`Do not use for more than 3 days`, `for no more than 3 days`): counted like `for 3 days` from the first day, and the note quotes it. When the label doesn't say which day to start, the first day is the usual one from the built-in list (or chemo day) and a note says so. The limit never sets the start day.
- **A number with no 0 before the point** (`.5 mg`, `.5 tablet`): read as 0.5, the way ISMP says it's meant. Writing 0.5 is safer, because `.5` is easy to misread as 5.
- **A time after midnight** (`at 12:30 AM on days 2, 3 and 4`): a planned time belongs to the start of its day, so 12:30 AM on Day 2 is the night between Day 1 and Day 2. The note says which night it means, and which days to pick if the label meant the night after. Premedication like `12 and 6 hours before chemo` (3 AM on Day 1) relies on this.
- **Not in the built-in list:** added as your own medicine under the name you typed.

Bottle labels also carry the patient's name, address, Rx number, pharmacy, prescriber, quantity, refills and NDC. Those lines are dropped before reading, and the pasted text is never stored. Only the medicine details are saved.

## Sources

- NCPDP Structured and Codified Sig format, as evaluated in Liu et al., *JAMIA* 2011. <https://pmc.ncbi.nlm.nih.gov/articles/PMC3168301/>
- Karystianis et al., "Modelling and extraction of variability in free-text medication prescriptions", *BMC Med Inform Decis Mak* 2016 (dose and frequency ranges, abbreviations, misspellings). <https://pmc.ncbi.nlm.nih.gov/articles/PMC4748480/>
- AHRQ, Explicit and Standardized Prescription Medicine Instructions (Universal Medication Schedule). <https://www.ahrq.gov/health-literacy/improve/pharmacy/instructions.html>
- ISMP List of Error-Prone Abbreviations, Symbols, and Dose Designations. <https://www.ismp.org/system/files/resources/2024-04/ISMP_ErrorProneAbbreviation_List.pdf>
- FDA and ISMP Lists of Look-Alike Drug Names with Recommended Tall Man Letters. <https://www.ismp.org/system/files/resources/2023-01/ISMP_Tallman_Letters_012523_MS5087%20%282%29.pdf>
- BC Cancer protocol SCNAUSEA (antiemetics: "olanzapine ... daily on days 2, 3 and 4", "prochlorperazine 10 mg PO every 6 hours PRN x 3 to 4 days"). <https://www.bccancer.bc.ca/chemotherapy-protocols-site/Documents/Supportive%20Care/SCNAUSEA_Protocol.pdf>
- BC Cancer protocol BRAJACTG (AC then paclitaxel: "Repeat every 14 days x 4 cycles", "filgrastim ... on Days 2 to 8", "pegfilgrastim 6 mg on Day 2"). <https://www.bccancer.bc.ca/chemotherapy-protocols-site/Documents/Breast/BRAJACTG_Protocol.pdf>
- eviQ 4010, AC dose dense followed by paclitaxel weekly ("Frequency: 14 days, Cycles: 4" then "Frequency: 7 days, Cycles: 12"). <https://www.eviq.org.au/medical-oncology/breast/neoadjuvant-adjuvant/4010-breast-adjuvant-ac-doxorubicin-and-cyclophos>
- eviQ patient information, capecitabine ("Day 1 to 14 ... This treatment cycle is repeated every 21 days"). <https://www.eviq.org.au/medical-oncology/colorectal/adjuvant-and-neoadjuvant/74-colorectal-adjuvant-capecitabine/patient-information>
- Living Beyond Breast Cancer, AC-T regimen schedules. <https://www.lbbc.org/about-breast-cancer/treatments/chemotherapy/common-regimens/ac-t-doxorubicin>
- CION, How to read your chemotherapy plan sheet ("Day numbers reset each cycle", q3w / q21d / D1, D8). <https://www.cioncancerclinics.com/chemotherapy/understanding/how-to-read-your-chemotherapy-plan/>
- Cancer Research UK, Treatment cycles and courses. <https://www.cancerresearchuk.org/about-cancer/treatment/cancer-drugs/how-you-have/treatment-cycles-and-courses>
- MedlinePlus, Fentanyl Transdermal Patch ("Apply the patch to the skin once every 72 hours", "Do not ... apply the patches more often ... than prescribed"). <https://medlineplus.gov/druginfo/meds/a601202.html>
- DailyMed, paclitaxel label (premedication "dexamethasone 20 mg PO administered approximately 12 and 6 hours before paclitaxel"). <https://www.dailymed.nlm.nih.gov/dailymed/fda/fdaDrugXsl.cfm?setid=5721b9bf-c3c1-4b73-b96c-5fbe3147b6a5>
- DailyMed, Neulasta label (the on-body injector "is programmed to deliver your dose about 27 hours after your healthcare provider places" it, and the clinic lets it reach room temperature before putting it on). <https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=fdfe5d72-6b80-435a-afa4-c5d74dd852ce>
- DailyMed, Tylenol Regular Strength label ("maximum daily dose ... 10 tablets (3,250 mg) in 24 hours": a daily limit means 24 hours). <https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=1622f694-4d63-4c56-8737-fae31f0ecfb7>
