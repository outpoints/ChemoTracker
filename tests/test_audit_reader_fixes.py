"""Reader leftovers from the safety audit: a naked decimal point (".5 tablet") and "do not use for more than
N days", which used to be dropped (and the preview then said the days weren't on the label)."""

from __future__ import annotations

from chemotracker import rxparse


def test_reader_reads_a_leading_dot_as_a_half():
    [m] = rxparse.parse("Lorazepam .5 MG TAB - Take .5 tablet by mouth every 6 hours as needed for nausea")
    assert m.strength == "0.5 mg tablet" and m.dose and m.dose.startswith("½ tablet")
    [m] = rxparse.parse("Lorazepam - Take .5 mg by mouth at bedtime as needed for sleep")
    assert m.dose == "0.5 mg"


def test_reader_reads_do_not_use_for_more_than_n_days_as_the_day_limit():
    # As needed: the limit counts from the first dose, not a cycle day, so the usual days stay and the label is quoted.
    # Capping the days would call a first rescue dose on Day 4 "not planned".
    [m] = rxparse.parse("Prochlorperazine 10 MG TAB - Take 1 tablet by mouth every 6 hours as needed for nausea. "
                        "Do not use for more than 3 days.")
    p = m.plans[0]
    assert p.kind == "as_needed" and p.end_day != p.start_day + 2
    assert not any("Days aren't on the label" in n for n in m.notes), m.notes
    assert any('"Do not use for more than 3 days". That counts from the first dose' in n for n in m.notes), m.notes
    # With a start on the label, the limit sets the last day. Scheduled medicines read it too.
    [m] = rxparse.parse("Dexamethasone 4 MG TAB - Take 1 tablet by mouth twice daily starting the day after chemo. "
                        "Do not take for more than 3 days.")
    assert [(p.kind, p.start_day, p.end_day) for p in m.plans] == [("scheduled", 2, 4)]
    assert any('"Do not take for more than 3 days"' in n and "Day 4" in n for n in m.notes), m.notes


def test_reader_explains_which_night_a_time_after_midnight_means():
    [m] = rxparse.parse("Olanzapine 5 MG TAB - Take 1 tablet by mouth at 12:30 AM on days 2, 3 and 4")
    assert [(p.start_day, p.end_day, p.times) for p in m.plans] == [(2, 4, ["00:30"])]  # dated as before
    assert ("A time after midnight belongs to the start of that day. 12:30 AM on Day 2 is the night between Day 1 and "
            "Day 2. If you meant the night after Day 2, choose Days 3 to 5 after adding it.") in m.notes
    # Premedication counted back from chemo keeps its night and gets no "if you meant" advice.
    [m] = rxparse.parse("Dexamethasone 4 MG TAB - Take 5 tablets 12 and 6 hours before chemo")
    assert [(p.start_day, p.times) for p in m.plans] == [(0, ["21:00"]), (1, ["03:00"])]
    night = [n for n in m.notes if n.startswith("A time after midnight")]
    assert night == ["A time after midnight belongs to the start of that day. 3:00 AM on Day 1 is the night between "
                     "Day 0 and Day 1."]
    [m] = rxparse.parse("Prochlorperazine 10 MG TAB - Take 1 tablet every 6 hours as needed for nausea for no more than 3 days")
    assert any('"For no more than 3 days". That counts from the first dose' in n for n in m.notes), m.notes
