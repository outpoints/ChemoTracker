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
| Dose | `Take one tablet`, `2 tablets`, `1-2 tablets`, `½ tablet`, `8 mg` |
| Route | `by mouth`, `PO`, `under the skin`, `SUBQ`, `IV`, `on the tongue`, `apply` |
| How often | `daily`, `twice daily`, `BID`, `TID`, `QID`, `every 8 hours`, `q6h`, `every 4 to 6 hours` |
| When | `at bedtime`, `QHS`, `in the morning`, `with meals`, `at 9 PM` |
| As needed | `as needed`, `PRN`, `if needed`, `after each loose stool`, plus what for (`for nausea or vomiting`, `n/v`) |
| Limit | `do not exceed 8 tablets in 24 hours`, `no more than 4 a day` |
| Duration | `for 3 days`, `x 3 to 4 days` |
| Steps | `then` (tapers like `4 mg the evening of chemo, then twice daily for 3 days`), `then stop` |

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

## Default times

When the label gives no clock time, the app uses the Universal Medication Schedule periods, the same Morning, Noon, Evening and Bedtime compartments Today shows:

| Directions | Times |
|---|---|
| once daily, every morning | 8 AM (or the medicine's usual time, e.g. olanzapine at bedtime) |
| twice daily, every 12 hours | 8 AM, 8 PM |
| three times daily | 8 AM, 2 PM, 8 PM |
| four times daily | 8 AM, 12 PM, 5 PM, 9 PM |
| with meals | 8 AM, 12 PM, 6 PM |
| at bedtime | 9 PM |

## When the label is incomplete

The reader never guesses silently. Each guess shows a note and a "guess" tag:

- **No days on the label** (common for rescue medicines): uses the medicine's usual days from the built-in list and says so.
- **"Every 8 hours" without "as needed"** for a rescue anti-nausea medicine: sets it as needed with an 8-hour gap and says so. These are commonly prescribed that way after chemo. Switch it to "On a schedule" if you were told to take it around the clock.
- **Ranges** (`every 4 to 6 hours`, `x 3 to 4 days`): uses the shorter gap and the longer number of days, and says so.
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
