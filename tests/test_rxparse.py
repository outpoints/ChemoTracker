"""Prescription text reader. Examples follow published label and protocol wording
(BC Cancer SCNAUSEA, eviQ, USP <17> / Universal Medication Schedule). No real patient data."""

from __future__ import annotations

import pytest

from chemotracker import rxparse
from chemotracker.web import RX_EXAMPLE


def one(text: str) -> rxparse.ParsedMed:
    meds = rxparse.parse(text)
    assert len(meds) == 1, [m.name for m in meds]
    return meds[0]


def spans(m: rxparse.ParsedMed) -> list[tuple[str, int, int | None, list[str]]]:
    return [(p.kind, p.start_day, p.end_day, p.times) for p in m.plans]


def test_in_app_example_reads_as_four_medicines():
    meds = rxparse.parse(RX_EXAMPLE)
    assert [m.info_key for m in meds] == ["ondansetron", "prochlorperazine", "dexamethasone", "olanzapine"]
    ond, pro, dex, olz = meds
    assert (ond.strength, ond.dose, ond.min_hours_between, ond.kind) == ("8 mg tablet", "1 tablet (8 mg)", 8, "as_needed")
    assert any('not "as needed"' in n for n in ond.notes)
    assert pro.min_hours_between == 6 and pro.kind == "as_needed"
    assert (dex.dose, spans(dex)) == ("2 tablets (8 mg)", [("scheduled", 2, 4, ["08:00"])])
    assert (olz.dose, spans(olz), olz.plans[0].label) == ("1 tablet (5 mg)", [("scheduled", 2, 4, ["21:00"])], "At bedtime")
    assert olz.directions == "Take 1 tablet by mouth at bedtime. Take on days 2, 3 and 4 after chemo then stop"


@pytest.mark.parametrize("text,key", [
    ("dexAMETHasone 4MG TAB", "dexamethasone"),          # tall man letters
    ("OLANZapine 5 MG TAB TEVA", "olanzapine"),         # maker code
    ("Ondansetron HCl 4 mg ODT", "ondansetron"),         # salt + form
    ("Prochlorperazine Maleate 10 MG TAB", "prochlorperazine"),
    ("Zofran 8 mg", "ondansetron"),                      # brand
    ("Neulasta Onpro 6 mg", "pegfilgrastim"),
    ("Ondansetrone 8mg", "ondansetron"),                 # misspelling
    ("Lidocaine-prilocaine 2.5%-2.5% cream", "lidocaine-prilocaine"),
    ("Emend 125 mg capsule", "aprepitant-oral"),
    ("Fosaprepitant 150 mg IV", "aprepitant"),
])
def test_drug_names(text, key):
    assert one(text + " - Take 1 by mouth daily").info_key == key


def test_protocol_style_lines():
    olz = one("olanzapine 5 mg PO daily on days 2, 3 and 4")
    assert olz.dose == "5 mg" and spans(olz) == [("scheduled", 2, 4, ["21:00"])]
    pro = one("prochlorperazine 10 mg PO every 6 hours PRN x 3 to 4 days")
    assert pro.kind == "as_needed" and pro.min_hours_between == 6 and (pro.plans[0].start_day, pro.plans[0].end_day) == (1, 4)
    assert any("range of days" in n for n in pro.notes) and any("Start day" in n for n in pro.notes)
    meto = one("metoclopramide 10 to 20 mg PO every 4 to 6 hours PRN x 3 to 4 days")
    assert meto.dose == "10–20 mg" and meto.min_hours_between == 4
    assert one("aprepitant 80 mg PO daily on days 2 and 3").plans[0].days == [2, 3]


def test_tapers_become_separate_schedules():
    dex = one("dexamethasone 4 mg PO evening of chemo, then PO BID x 2 to 3 days")
    assert spans(dex) == [("scheduled", 1, 1, ["18:00"]), ("scheduled", 2, 4, ["08:00", "20:00"])]
    tap = one("Dexamethasone 4 mg tab - Take 2 tablets by mouth twice daily for 2 days, then 1 tablet twice daily for 2 days, then stop")
    assert [(p.start_day, p.end_day, p.dose) for p in tap.plans] == [(2, 3, "2 tablets (8 mg)"), (4, 5, "1 tablet (4 mg)")]


@pytest.mark.parametrize("sig,expected", [
    ("Inject 300 mcg under the skin once daily on days 2 to 8", [("scheduled", 2, 8)]),
    ("Inject 6 mg under the skin once on day 2", [("scheduled", 2, 2)]),
    ("Take 2 tablets by mouth twice daily for 3 days starting the day before chemotherapy", [("scheduled", 0, 2)]),
    ("Take 1 tablet by mouth daily for 3 days starting the day after chemo", [("scheduled", 2, 4)]),
    ("Inject 1 syringe under the skin daily for 7 days starting 24 hours after chemo", [("scheduled", 2, 8)]),
    ("Take 1 capsule by mouth 1 hour before chemo on day 1", [("scheduled", 1, 1)]),
    ("Take 3 tablets by mouth twice daily with food on days 1 through 14 of each 21-day cycle", [("scheduled", 1, 14)]),
    ("Take 1 tablet on days 1, 8 and 15", [("scheduled", 1, 1), ("scheduled", 8, 8), ("scheduled", 15, 15)]),
    ("Take 1 tablet every 8 hours as needed for nausea. Start on day 4 for 3 days", [("as_needed", 4, 6)]),
])
def test_cycle_day_anchors(sig, expected):
    m = one("Examplemide 10 mg tab - " + sig)
    assert [(p.kind, p.start_day, p.end_day) for p in m.plans] == expected


@pytest.mark.parametrize("sig,times", [
    ("Take 1 tablet by mouth twice daily", ["08:00", "20:00"]),
    ("Take 1 tablet by mouth three times a day", ["08:00", "14:00", "20:00"]),
    ("Take 1 tablet by mouth QID", ["08:00", "12:00", "17:00", "21:00"]),
    ("Take 1 tablet by mouth at bedtime", ["21:00"]),
    ("1 tab po qhs", ["21:00"]),
    ("Take 1 tablet in the morning", ["08:00"]),
    ("Take 1 tablet with meals", ["08:00", "12:00", "18:00"]),
    ("Take 1 tablet at 9 PM", ["21:00"]),
    ("Take 1 tablet every 12 hours", ["08:00", "20:00"]),
    ("Take 1 tablet every 8 hours around the clock", ["06:00", "14:00", "22:00"]),
])
def test_times_follow_universal_medication_schedule(sig, times):
    m = one("Examplemide 10 mg tab - " + sig)
    assert m.plans[0].kind == "scheduled" and m.plans[0].times == times


def test_as_needed_details():
    m = one("Zofran ODT 8 mg - Dissolve 1 tablet on the tongue every 8 hours as needed for nausea or vomiting")
    assert (m.strength, m.kind, m.indication, m.min_hours_between) == ("8 mg dissolving tablet", "as_needed", "nausea or vomiting", 8)
    assert m.plans[0].label == "As needed for nausea or vomiting"
    assert one("Ondansetron 8mg - 1 tab po q8h prn n/v").indication == "nausea or vomiting"
    apap = one("Acetaminophen 500 mg tab - Take 1-2 tablets by mouth every 6 hours as needed for pain. Do not take more than 6 tablets per day")
    assert apap.dose == "1–2 tablets (500–1000 mg)" and apap.max_per_day is None and any("Label limit" in n for n in apap.notes)
    fixed = one("Examplemide 5 mg tab - Take 1 tablet every 4 hours as needed. Do not exceed 4 tablets in 24 hours")
    assert fixed.max_per_day == 4
    lop = one("Loperamide 2 mg cap - Take 2 capsules by mouth after the first loose stool, then 1 capsule after each loose stool. "
              "Do not exceed 8 capsules in 24 hours")
    assert lop.kind == "as_needed" and len(lop.plans) == 1


def test_route_form_and_clinic():
    assert one("Filgrastim 300 mcg/0.5 mL syringe - Inject 300 mcg under the skin daily on days 2 to 8").route == "injection"
    assert one("Filgrastim 300 mcg/0.5 mL syringe - Inject daily").strength == "300 mcg/0.5 mL injection"
    assert one("Paclitaxel 80 mg/m2 IV on day 1").kind == "clinic"
    assert one("Lidocaine-prilocaine cream - Apply to port site 1 hour before chemo").route == "topical"


def test_unknown_medicine_keeps_its_name():
    m = one("Magic mouthwash - Swish and spit 10 mL every 4 hours as needed for mouth pain")
    assert (m.info_key, m.name, m.dose, m.kind) == (None, "Magic Mouthwash", "10 mL", "as_needed")
    assert any("own medicine" in n for n in m.notes)


def test_bottle_label_noise_is_dropped():
    label = """EXAMPLE PHARMACY #0000
123 Main St Springfield, IL 62701
(555) 010-0100
RX# 12345   Fill: 01/02/2030
SAMPLE, JOHN
TAKE 1 TABLET BY MOUTH AT BEDTIME. TAKE ON DAYS 2, 3, AND 4 AFTER CHEMO THEN STOP.
Discard After: 01/02/2031
OLANZapine 5 MG TAB NORT
Qty: 12  Refill 3
Prescriber: Dr. Example
NDC: 00000"""
    m = one(label)
    assert m.info_key == "olanzapine" and spans(m) == [("scheduled", 2, 4, ["21:00"])]
    kept = " ".join([m.source, m.directions or "", m.name])
    for leaked in ("SAMPLE", "JOHN", "Main St", "12345", "555", "EXAMPLE PHARMACY", "NDC", "Qty", "Prescriber"):
        assert leaked not in kept


def test_missing_days_use_catalog_defaults_with_a_note():
    m = one("Tamoxifen 20 mg tablet - Take 1 tablet by mouth every morning")
    assert spans(m) == [("scheduled", 1, None, ["08:00"])] and any("aren't on the label" in n for n in m.notes)
    assert not m.plans[0].from_label


def test_weird_input_does_not_crash():
    for text in ["", "   ", "hello", "Take 2", "8 mg", "every 6 hours", "💊💊", "Days 99 to 1", "q" * 500]:
        rxparse.parse(text)


# ---------------------------------------------------------------- safety audit: frequency, days, limits, units


@pytest.mark.parametrize("sig,expected", [
    # "N times a day" is a frequency, never "for 1 day"; named times that add up to the count are used.
    ("Take 1 tablet by mouth 4 times a day before meals and at bedtime for 5 days",
     [("scheduled", 1, 5, ["08:00", "12:00", "18:00", "21:00"])]),
    ("Swish and spit 15 mL four times a day", [("scheduled", 1, None, ["08:00", "12:00", "17:00", "21:00"])]),
    ("Take 1 tablet twice a day, morning and evening", [("scheduled", 1, None, ["08:00", "18:00"])]),
    # A count that disagrees with a named time keeps the count (and says so).
    ("Take 1 tablet twice daily at bedtime", [("scheduled", 1, None, ["08:00", "20:00"])]),
    # Day anchors joined by "and" or commas.
    ("Take 2 tablets twice daily the day before, the day of and the day after chemo", [("scheduled", 0, 2, ["08:00", "20:00"])]),
    ("Take 2 tablets the night before and 2 tablets the morning of chemo",
     [("scheduled", 0, 0, ["21:00"]), ("scheduled", 1, 1, ["08:00"])]),
    # Never a dose every day when the label spaces doses further apart.
    ("Apply 1 patch every 3 days", [("scheduled", 1, 1, ["08:00"])]),
    ("Take 1 tablet every other day", [("scheduled", 1, 1, ["08:00"])]),
    ("Take 1 tablet weekly", [("scheduled", 1, 1, ["08:00"])]),
    # The as-needed step after "then" carries on from the next day.
    ("Take 1 tablet every 8 hours for 2 days, then as needed",
     [("scheduled", 1, 2, ["06:00", "14:00", "22:00"]), ("as_needed", 3, None, [])]),
])
def test_frequency_and_days_follow_the_label(sig, expected):
    assert spans(one("Examplemide 10 mg tab - " + sig)) == expected


def test_guesses_the_reader_makes_come_with_a_note():
    notes = one("Examplemide 10 mg tab - Apply 1 patch every 3 days").notes
    assert any("every 3 days" in n and "only the first one" in n for n in notes)
    assert any("twice a day and also \"at bedtime\"" in n for n in one("Examplemide 10 mg tab - Take 1 tablet twice daily at bedtime").notes)
    assert any("Not on chemo day" in n for n in one("Examplemide 10 mg tab - Take 1 tablet daily, not on chemo day").notes)
    dex = one("Dexamethasone 4 mg tab - Take 5 tablets (20 mg) by mouth 12 and 6 hours before chemo")
    assert [(p.start_day, p.times) for p in dex.plans] == [(0, ["21:00"]), (1, ["03:00"])]
    assert any("guessed 9:00 AM" in n for n in dex.notes)


@pytest.mark.parametrize("sig,limit", [
    ("Take 1 tablet every 4 hours as needed, not more than 3 tablets a day", 3),
    ("Take 1 tablet every 4 hours as needed, never more than 3 tablets a day", 3),
    ("Take 1 tablet every 4 hours as needed, at most 3 tablets a day", 3),
    ("Take 1 tablet every 4 hours as needed. Max 3 tablets/day", 3),
    ("Take 1 tablet every 4 hours as needed, no more than 3 a day", 3),
    ("Take 2 tablets every 6 hours as needed. Do not exceed 8 tablets in 24 hours", 4),
    # A limit in mg becomes doses only when one dose is known in mg (10 mg tablets here).
    ("Take 1 tablet every 6 hours as needed. Do not exceed 40 mg per day", 4),
    ("Take 2 tablets every 6 hours as needed. Do not take more than 3,000 mg a day", 150),
    ("Take 20 mg every 6 hours as needed. Do not exceed 60 mg per day", 3),
    ("Take 1 tablet every 6 hours as needed. Do not exceed 1 g per day", 100),
    # Can't be counted: dose varies, or one dose has no known mg. Left empty with a note.
    ("Take 1-2 tablets every 6 hours as needed. Do not exceed 60 mg per day", None),
    ("Take 5 mL every 6 hours as needed. Do not exceed 40 mg per day", None),
])
def test_daily_limits_are_counted_in_doses(sig, limit):
    m = one("Examplemide 10 mg tab - " + sig)
    assert m.max_per_day == limit
    if limit is None:
        assert any("Label limit" in n for n in m.notes)


def test_a_dose_unit_that_contradicts_the_strength_is_left_empty():
    m = one("Filgrastim 300 mcg/0.5 mL syringe - Inject 300 mg under the skin daily on days 2 to 8")
    assert m.dose is None and any("300 mcg" in n and "300 mg" in n for n in m.notes)
    assert one("Examplemide 500 mg tab - Take 0.5 g by mouth daily").dose == "0.5 g"
    assert not any("strength" in n for n in one("Examplemide 500 mg tab - Take 0.5 g by mouth daily").notes)


def test_on_body_injector_has_no_fridge_step():
    m = one("Pegfilgrastim 6 mg/0.6 mL on-body injector - Delivers the dose about 27 hours after it is applied")
    assert (m.brand, m.no_prep, m.prep_minutes, m.plans[0].label) == ("Neulasta Onpro", True, None, "On-body injector")
    syringe = one("Pegfilgrastim 6 mg/0.6 mL syringe - Inject 6 mg under the skin once on day 2")
    assert not syringe.no_prep and syringe.brand == "Neulasta"
