"""Audit: changing the schedule or the cycles after doses exist.

Each test reproduces one finding in audit-findings/schedule.md. A test FAILS on
the audited code (v0.5.1, commit 624de25) for the reason named in its docstring. Demo names only.

Dates are always in the past (yesterday or earlier), so the results don't depend on the time of day the tests run.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta

from chemotracker import export, reminders, schedule, store
from chemotracker.db import session

from .conftest import local_now, setup_walkthrough, today


def _plan(conn, med_name, kind="scheduled"):
    return conn.execute("""SELECT p.* FROM plans p JOIN medications m ON m.id = p.medication_id
                            WHERE m.name = ? AND p.kind = ? ORDER BY p.id""", (med_name, kind)).fetchone()


def _med(conn, name):
    return conn.execute("SELECT id FROM medications WHERE name = ?", (name,)).fetchone()["id"]


def _slots(conn, d, med_name):
    cycle = schedule.cycle_for_date(conn, d)
    return [s for s in schedule.slots(conn, cycle, schedule.day_number(cycle, d)) if s.plan["med_name"] == med_name]


def _taken(conn, med_name, d):
    return conn.execute("""SELECT dl.* FROM dose_logs dl JOIN medications m ON m.id = dl.medication_id
                            WHERE m.name = ? AND dl.status = 'taken' AND substr(dl.taken_at, 1, 10) = ?""",
                        (med_name, d.isoformat())).fetchall()


def _at(d, hh, mm=0):
    return datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm)


# ---------------------------------------------------------------- F1: back-to-back cycles hide the last day


def test_F1_last_day_of_a_cycle_stays_on_today_when_the_next_cycle_starts_the_next_day(client, cfg):
    """Cycle 2's Day 0 is cycle 1's last day. Today, "From last night" and Taken only look at cycle 2 that day,
    so cycle 1's last-day doses vanish from Today while reminders still announce them."""
    t = today(cfg)
    y = t - timedelta(days=1)
    setup_walkthrough(client, t - timedelta(days=14))  # yesterday = cycle 1 Day 14
    with session(cfg.db_path) as conn:
        olz = _plan(conn, "Olanzapine")
    # Olanzapine at bedtime every day of the cycle (a daily medicine, through the real schedule form).
    r = client.post(f"/plans/{olz['id']}", data={"kind": "scheduled", "start_day": "1", "end_day": "", "times": "21:00",
                                                "label": "At bedtime", "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    # Cycle 2 starts today, 14 days after cycle 1, the usual "every 14 days" (yesterday is its Day 0).
    r = client.post("/cycles", data={"number": "2", "day1": t.isoformat(), "length_days": "14", "count": "1"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    problems = []
    with session(cfg.db_path) as conn:
        assert store.plan(conn, olz["id"])["end_day"] is None
        c1 = conn.execute("SELECT * FROM cycles WHERE number = 1").fetchone()
        assert schedule.day_number(c1, y) == 14
        assert [s.time for s in schedule.slots(conn, c1, 14) if s.plan["med_name"] == "Olanzapine"] == ["21:00"]
        # Reminders announce it and the report calls it "Not logged" ...
        assert any(s.plan["med_name"] == "Olanzapine" for s in reminders._slots_on(conn, y))
        report = export.dose_rows(conn, y, y, local_now(cfg))
        assert any(r.medicine.startswith("Olanzapine") and r.status == "Not logged" for r in report)
        # ... so Today must show it that day, and "From last night" must have it the next morning.
        shown = [s for s in schedule.day_plan(conn, y, local_now(cfg)).scheduled if s.plan["med_name"] == "Olanzapine"]
        if not shown:
            problems.append("Today (and From last night) for cycle 1 Day 14 has no Olanzapine row")
        olz_id = olz["id"]
    if "Olanzapine" not in client.get(f"/?d={y.isoformat()}").text.split('id="extra-doses"')[0]:
        problems.append("the Today page for that day doesn't list Olanzapine")
    r = client.post("/slot", data={"plan_id": olz_id, "slot": f"{y.isoformat()} 21:00", "status": "taken",
                                   "taken_at": f"{y.isoformat()}T21:05", "back": "/"}, follow_redirects=False)
    if r.status_code == 404:
        problems.append("Taken on the reminded dose answers 404 'isn't on the schedule any more'")
    with session(cfg.db_path) as conn:
        med = _med(conn, "Olanzapine")
    r = client.post("/dose/new", data={"medication_id": med, "taken_at": f"{y.isoformat()}T21:05", "back": "/"})
    if "planned today" in html.unescape(r.text) or "planned yesterday" in html.unescape(r.text):
        problems.append("Log a dose for it warns that Olanzapine isn't planned that day")
    assert not problems, problems


# ---------------------------------------------------------------- F2: small-hours dose fills tonight's slot


def test_F2_log_a_dose_after_midnight_counts_for_last_nights_bedtime_dose_not_tonights(client, cfg):
    """A dose logged at 12:30 AM with "Log a dose" is paired to the same day's 9 PM slot (20.5 h away) instead of
    last night's 9 PM slot (3.5 h away), because _pair_logs sorts by day index before time gap."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=3))  # Olanzapine 9 PM bedtime, Days 2-4
    d2, d3 = t - timedelta(days=2), t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        med = _med(conn, "Olanzapine")
    r = client.post("/dose/new", data={"medication_id": med, "taken_at": f"{d3.isoformat()}T00:30", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303, "nothing else is logged, so no warning is expected"
    with session(cfg.db_path) as conn:
        night2 = _slots(conn, d2, "Olanzapine")[0]
        night3 = _slots(conn, d3, "Olanzapine")[0]
    assert night2.log is not None and night3.log is None, (
        f"12:30 AM dose went to {'Day 3 9 PM (tonight)' if night3.log else 'nowhere'}; "
        "Day 3's bedtime dose now shows Taken 12:30 AM and would be skipped")


# ---------------------------------------------------------------- F3: Took both = two of one medicine at once


def test_F3_took_both_with_two_doses_of_one_medicine_asks_first(client, cfg):
    """"Took both" checks each slot with dose_warning before any is written, so two doses of the same medicine
    (a late 8 AM and the 11 AM) are logged at the same minute. Taken one by one, the second asks first."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))  # yesterday = Day 2: Dexamethasone
    y = t - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        plan = _plan(conn, "Dexamethasone")
        conn.execute("UPDATE plans SET times = '08:00,11:00' WHERE id = ?", (plan["id"],))
    with session(cfg.db_path) as conn:
        morning = next(c for c in schedule.day_plan(conn, y, local_now(cfg)).compartments if c.key == "morning")
        offered = morning.open_now(local_now(cfg))
        assert [s.plan["med_name"] for s in offered] == ["Dexamethasone", "Dexamethasone"]  # the "Took both" button
        items = [f"{s.plan['id']}|{s.key}" for s in offered]
    client.post("/slots/taken", data={"slots[]": items, "when": "at", "at_time": "11:05", "back": "/"})
    with session(cfg.db_path) as conn:
        logs = _taken(conn, "Dexamethasone", y)
    assert len(logs) == 1, f"Took both logged {len(logs)} Dexamethasone doses at the same minute without asking"


# ---------------------------------------------------------------- F4: two schedules at the same time


def test_F4_a_duplicate_schedule_does_not_switch_off_the_second_dose_check(client, cfg):
    """Two schedules of one medicine at the same time (the Add schedule form sent twice) make the planned gap 0,
    and dose_warning's "less than half the planned gap" test can never fire. A second shot an hour later is
    logged without a warning."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))  # yesterday = Day 2: Filgrastim 3 PM, Days 2-7
    y = t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        med = _med(conn, "Filgrastim")
    r = client.post(f"/medicines/{med}/plans", data={"kind": "scheduled", "start_day": "2", "end_day": "7",
                                                     "times": "15:00", "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        rows = _slots(conn, y, "Filgrastim")
    assert [s.time for s in rows] == ["15:00", "15:00"]  # Today shows the shot twice
    a, b = rows
    r = client.post("/slot", data={"plan_id": a.plan["id"], "slot": a.key, "status": "taken",
                                   "taken_at": f"{y.isoformat()}T15:00", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/slot", data={"plan_id": b.plan["id"], "slot": b.key, "status": "taken",
                                   "taken_at": f"{y.isoformat()}T16:00", "back": "/"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        n = len(_taken(conn, "Filgrastim", y))
    assert r.status_code == 200 and "Check before you log this" in r.text and n == 1, \
        f"second Filgrastim shot logged without a warning (status {r.status_code}, {n} shots that day)"


# ---------------------------------------------------------------- F5: cycle sheet files doses by taken date


def test_F5_cycle_sheet_shows_a_bedtime_dose_on_its_planned_day(client, cfg):
    """grid() files every log under the day it was TAKEN, but pairs it to the slot of the day it was PLANNED.
    Last night's 9 PM dose taken at 12:30 AM shows on the next day, and that day looks done."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=3))  # Day 2 two days ago, Day 3 yesterday
    d2, d3 = t - timedelta(days=2), t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Olanzapine")
        cycle_id = schedule.cycle_for_date(conn, d2)["id"]
    r = client.post("/slot", data={"plan_id": plan["id"], "slot": f"{d2.isoformat()} 21:00", "status": "taken",
                                   "taken_at": f"{d3.isoformat()}T00:30", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    problems = []
    with session(cfg.db_path) as conn:
        assert _slots(conn, d2, "Olanzapine")[0].log is not None and _slots(conn, d3, "Olanzapine")[0].log is None
        _, rows, _ = schedule.grid(conn, schedule.cycle_for_date(conn, d2), local_now(cfg))
        cells = next(r for r in rows if r.name == "Olanzapine").cells
    if len(cells[2].taken) != 1:
        problems.append(f"Day 2 cell: planned {cells[2].planned}, taken {len(cells[2].taken)}, missing {cells[2].missing}")
    if cells[3].taken:
        problems.append(f"Day 3 cell shows the Day 2 dose as taken (missing {cells[3].missing})")
    row = html.unescape(client.get(f"/cycles/{cycle_id}?day=3").text).split(">Olanzapine</a>")[1].split("</li>")[0]
    if "Not logged" not in row:
        problems.append("Cycle day list for Day 3 shows Olanzapine with a check, not 'Not logged'")
    assert not problems, problems


# ---------------------------------------------------------------- F6: day list hides a missing second dose


def test_F6_cycle_day_list_says_when_one_of_two_doses_is_not_logged(client, cfg):
    """The cycle page's day list shows a check and the logged time whenever ANY dose of the medicine that day is
    logged (template checks c.taken before c.missing), so a missed evening dose is hidden there."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))  # yesterday = Day 2
    y = t - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        plan = _plan(conn, "Dexamethasone")
        conn.execute("UPDATE plans SET times = '08:00,20:00' WHERE id = ?", (plan["id"],))
        plan = store.plan(conn, plan["id"])
        store.log_slot(conn, plan, f"{y.isoformat()} 08:00", "taken", _at(y, 8, 5), None)
        cycle_id = schedule.cycle_for_date(conn, y)["id"]
    with session(cfg.db_path) as conn:
        _, rows, _ = schedule.grid(conn, schedule.cycle_for_date(conn, y), local_now(cfg))
        cell = next(r for r in rows if r.name == "Dexamethasone").cells[2]
        assert cell.missing == 1 and len(cell.taken) == 1  # the data knows the 8 PM dose is missing
    row = html.unescape(client.get(f"/cycles/{cycle_id}?day=2").text).split(">Dexamethasone</a>")[1].split("</li>")[0]
    assert "Not logged" in row or "1 of 2" in row, "day list shows only '8:05 AM' with a check; the missed 8 PM dose is hidden"


# ---------------------------------------------------------------- F7: doses after the cycle's last day


def test_F7_a_dose_logged_after_the_cycle_ends_still_shows_on_today(client, cfg):
    """Between cycles (chemo delayed, next cycle not added yet) Today still uses the old cycle, but its dose window
    ends 2 days after the cycle's last day, so doses logged later appear nowhere on Today."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=19))  # yesterday = Day 19 of a 14-day cycle
    y = t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        dex, ond = _med(conn, "Dexamethasone"), _med(conn, "Ondansetron")
    # Ondansetron as needed from "Other as-needed medicines", and a Dexamethasone the team asked for.
    r = client.post("/dose/new", data={"medication_id": ond, "taken_at": f"{y.isoformat()}T09:00", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303
    client.post("/dose/new", data={"medication_id": dex, "taken_at": f"{y.isoformat()}T09:30", "confirm": "1",
                                   "back": "/"})
    with session(cfg.db_path) as conn:
        assert len(_taken(conn, "Ondansetron", y)) == 1 and len(_taken(conn, "Dexamethasone", y)) == 1
        c = schedule.cycle_for_date(conn, y)
        assert c is not None and schedule.day_number(c, y) == 19  # Today still files the day under cycle 1
    page = client.get(f"/?d={y.isoformat()}").text
    extra = page.split('id="extra-doses"')[1].split("</section>")[0] if 'id="extra-doses"' in page else ""
    assert "Ondansetron" in extra and "Dexamethasone" in extra, "doses logged on Day 19 appear nowhere on Today"


# ---------------------------------------------------------------- F8: grid drops doses of medicines without a row


def test_F8_cycle_sheet_keeps_a_dose_after_its_schedule_moves_to_later_cycles(client, cfg):
    """grid() only has rows for plans active in the cycle. When a schedule is changed to start from cycle 2, or the
    medicine is stopped, its logged doses vanish from cycle 1's sheet (Today still lists them as Also logged)."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))  # yesterday = Day 2: Filgrastim 3 PM
    y = t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    r = client.post("/slot", data={"plan_id": plan["id"], "slot": f"{y.isoformat()} 15:00", "status": "taken",
                                   "taken_at": f"{y.isoformat()}T15:02", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/plans/{plan['id']}", data={"kind": "scheduled", "start_day": "2", "end_day": "7", "times": "15:00",
                                                 "from_cycle": "2"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/?d={y.isoformat()}").text
    assert "Filgrastim" in page.split('id="extra-doses"')[1].split("</section>")[0]  # Today keeps it
    with session(cfg.db_path) as conn:
        _, rows, _ = schedule.grid(conn, schedule.cycle_for_date(conn, y), local_now(cfg))
    on_sheet = [log for r in rows for log in r.cells[2].taken if r.medication_id == plan["medication_id"]]
    assert on_sheet, "the Filgrastim shot given on Day 2 is not on cycle 1's sheet any more"


# ---------------------------------------------------------------- F9: one dose fills a slot in two cycles


def _every_day_olanzapine(client, cfg):
    with session(cfg.db_path) as conn:
        olz = _plan(conn, "Olanzapine")
    r = client.post(f"/plans/{olz['id']}", data={"kind": "scheduled", "start_day": "1", "end_day": "", "times": "21:00",
                                                "label": "At bedtime", "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return olz


def test_F9_one_logged_dose_never_counts_in_two_cycles(client, cfg):
    """Each cycle pairs logs on its own, and their log windows overlap (cycle 1 reads 2 days past its end), so one
    12:30 AM dose fills cycle 1's last bedtime slot AND cycle 2's Day 1 bedtime slot."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=15))  # cycle 1 Day 14 = two days ago
    _every_day_olanzapine(client, cfg)
    r = client.post("/cycles", data={"number": "2", "day1": (t - timedelta(days=1)).isoformat(), "length_days": "14",
                                     "count": "1"}, follow_redirects=False)
    assert r.status_code == 303  # cycle 2 Day 1 = yesterday, its Day 0 = cycle 1 Day 14
    d14, d1 = t - timedelta(days=2), t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        med = _med(conn, "Olanzapine")
    r = client.post("/dose/new", data={"medication_id": med, "taken_at": f"{d1.isoformat()}T00:30", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        c1 = conn.execute("SELECT * FROM cycles WHERE number = 1").fetchone()
        c2 = conn.execute("SELECT * FROM cycles WHERE number = 2").fetchone()
        log_id = _taken(conn, "Olanzapine", d1)[0]["id"]
        holders = [(c["number"], s.day, s.time) for c in (c1, c2) for s in schedule.slots(conn, c)
                   if s.plan["med_name"] == "Olanzapine" and s.log is not None and s.log["id"] == log_id]
    assert len(holders) == 1, f"one logged dose fills {holders}: cycle 2 Day 1's 9 PM shows Taken 12:30 AM"


# ---------------------------------------------------------------- F10: an as-needed dose fills a scheduled slot


def _prn_then_scheduled(client, cfg):
    """Prochlorperazine: as needed Days 1-6 (walkthrough). Yesterday (Day 2) John took one at 9 AM with "Took one"."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))
    y = t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        prn = _plan(conn, "Prochlorperazine", "as_needed")
    r = client.post("/dose/quick", data={"medication_id": prn["medication_id"], "plan_id": prn["id"],
                                         "taken_at": f"{y.isoformat()}T09:00", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    return prn, y


def test_F10_changing_as_needed_to_scheduled_keeps_tonights_dose_open(client, cfg):
    """The clinic says: from today, take it every evening at 8 PM instead of as needed. Editing the plan's kind
    turns the morning's as-needed dose into tonight's 8 PM dose ("Taken 9:00 AM"), so tonight's is skipped."""
    prn, y = _prn_then_scheduled(client, cfg)
    r = client.post(f"/plans/{prn['id']}", data={"kind": "scheduled", "start_day": "2", "end_day": "6", "times": "20:00",
                                                "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        evening = _slots(conn, y, "Prochlorperazine")
    assert [s.time for s in evening] == ["20:00"]
    assert evening[0].log is None, "the 9 AM as-needed dose now counts as the 8 PM scheduled dose"


def test_F10b_deleting_the_as_needed_schedule_keeps_tonights_dose_open(client, cfg):
    """Same, when the as-needed schedule is deleted next to a separate 8 PM schedule: plan_id is set to NULL on
    delete, so the "Took one" dose looks like a "Log a dose" entry and is paired to the scheduled slot."""
    prn, y = _prn_then_scheduled(client, cfg)
    r = client.post(f"/medicines/{prn['medication_id']}/plans", data={"kind": "scheduled", "start_day": "2",
                                                                       "end_day": "6", "times": "20:00",
                                                                       "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        assert _slots(conn, y, "Prochlorperazine")[0].log is None  # by design while the as-needed plan exists
    client.post(f"/plans/{prn['id']}/delete", follow_redirects=False)
    with session(cfg.db_path) as conn:
        evening = _slots(conn, y, "Prochlorperazine")
    assert evening[0].log is None, "after deleting the as-needed schedule, the 9 AM dose fills the 8 PM row"


# ---------------------------------------------------------------- F11: scheduled -> as needed hides the dose


def test_F11_changing_scheduled_to_as_needed_keeps_the_logged_dose_on_today(client, cfg):
    """A dose logged on a scheduled plan that is then changed to as needed on later days is skipped by _pair_logs
    (its plan is as-needed now), is not an extra, and its day has no as-needed shelf: it appears nowhere on Today."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))  # yesterday = Day 2: Dexamethasone 8 AM
    y = t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Dexamethasone")
    r = client.post("/slot", data={"plan_id": plan["id"], "slot": f"{y.isoformat()} 08:00", "status": "taken",
                                   "taken_at": f"{y.isoformat()}T08:05", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/plans/{plan['id']}", data={"kind": "as_needed", "start_day": "3", "end_day": "6",
                                                 "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303
    page = html.unescape(client.get(f"/?d={y.isoformat()}").text)
    assert "8:05 AM" in page, "the Dexamethasone given at 8:05 AM is not shown anywhere on that day"


# ---------------------------------------------------------------- F12: chemo delayed, daily medicines stop


def test_F12_delaying_chemo_keeps_every_day_medicines_planned_until_the_next_cycle(client, cfg):
    """Moving cycle 2's Day 1 a week later leaves cycle 1 running (Today says "Day 17"), but "every day of the cycle"
    schedules end at the cycle's length, so the delay days have no rows and no reminders."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=17))  # cycle 1 Day 1 = 17 days ago
    _every_day_olanzapine(client, cfg)
    r = client.post("/cycles", data={"number": "2", "day1": (t - timedelta(days=3)).isoformat(), "length_days": "14",
                                     "count": "1"}, follow_redirects=False)
    assert r.status_code == 303
    y = t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        c2 = conn.execute("SELECT * FROM cycles WHERE number = 2").fetchone()
        assert [s.date for s in _slots(conn, y, "Olanzapine")] == [y]  # planned while cycle 2 runs
    # Counts too low: chemo delayed a week, through the real cycle form.
    r = client.post(f"/cycles/{c2['id']}", data={"number": "2", "day1": (t + timedelta(days=4)).isoformat(),
                                                "length_days": "14"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        c = schedule.cycle_for_date(conn, y)
        assert c["number"] == 1 and schedule.day_number(c, y) == 17  # Today calls yesterday "Day 17" of cycle 1
        shown = [s for s in schedule.day_plan(conn, y, local_now(cfg)).scheduled if s.plan["med_name"] == "Olanzapine"]
        reminded = [s for s in reminders._slots_on(conn, y) if s.plan["med_name"] == "Olanzapine"]
    assert shown and reminded, "an every-day medicine has no row and no reminder on the delay days"


# ---------------------------------------------------------------- F13: "Took both" from a page opened before an edit


def _toast(page: str) -> str:
    return page.split('class="toast-text">')[1].split("</span>")[0] if 'class="toast-text">' in page else ""


def test_F13_took_both_never_drops_a_dose_silently_after_a_schedule_edit(client, cfg):
    """John's Today page was opened before Jane moved Filgrastim from 8:00 to 8:30. His "Took both" logs the
    Dexamethasone and silently drops the Filgrastim shot he gave: the toast doesn't mention it."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))  # yesterday = Day 2
    y = t - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        fil = _plan(conn, "Filgrastim")
        conn.execute("UPDATE plans SET times = '08:00' WHERE id = ?", (fil["id"],))
    with session(cfg.db_path) as conn:
        morning = next(c for c in schedule.day_plan(conn, y, local_now(cfg)).compartments if c.key == "morning")
        items = [f"{s.plan['id']}|{s.key}" for s in morning.open_now(local_now(cfg))]
        assert sorted(s.plan["med_name"] for s in morning.open_now(local_now(cfg))) == ["Dexamethasone", "Filgrastim"]
    r = client.post(f"/plans/{fil['id']}", data={"kind": "scheduled", "start_day": "2", "end_day": "7", "times": "08:30",
                                                "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/slots/taken", data={"slots[]": items, "when": "at", "at_time": "08:10",
                                          "back": f"/?d={y.isoformat()}"})
    toast = html.unescape(_toast(r.text))
    with session(cfg.db_path) as conn:
        shots = len(_taken(conn, "Filgrastim", y))
    assert shots == 1 or "Filgrastim" in toast, f"Filgrastim not logged and not mentioned; the toast says: {toast!r}"


# ---------------------------------------------------------------- F14: reminder lists one dose twice


def test_F14_a_reminder_never_lists_the_same_dose_twice(client, cfg):
    """A schedule running "Day 0 to end of cycle" plans the same dose (same plan, same minute) on cycle 1's last day
    and cycle 2's Day 0. _slots_on collects both cycles, so the notification reads "Olanzapine 1 tablet" twice."""
    t = today(cfg)
    y = t - timedelta(days=1)
    setup_walkthrough(client, t - timedelta(days=14))  # yesterday = cycle 1 Day 14
    with session(cfg.db_path) as conn:
        olz = _plan(conn, "Olanzapine")
    r = client.post(f"/plans/{olz['id']}", data={"kind": "scheduled", "start_day": "0", "end_day": "", "times": "21:00",
                                                "label": "At bedtime", "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/cycles", data={"number": "2", "day1": t.isoformat(), "length_days": "14", "count": "1"})
    with session(cfg.db_path) as conn:
        out = reminders.due_reminders(conn, _at(y, 21, 0), {})
    due = next(r for r in out if r.key.startswith("due|"))
    assert due.message.count("Olanzapine") == 1, f"reminder reads: {due.message!r}"


# ---------------------------------------------------------------- controls: cases checked and found fine


def test_ok_moving_days_after_a_dose_keeps_it_visible_and_asks_before_another(client, cfg):
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))  # yesterday = Day 2: Filgrastim 3 PM, Days 2-7
    y = t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    client.post("/slot", data={"plan_id": plan["id"], "slot": f"{y.isoformat()} 15:00", "status": "taken",
                               "taken_at": f"{y.isoformat()}T15:02", "back": "/"}, follow_redirects=False)
    client.post(f"/plans/{plan['id']}", data={"kind": "scheduled", "start_day": "3", "end_day": "8", "times": "15:00",
                                             "from_cycle": "1"}, follow_redirects=False)
    extra = html.unescape(client.get(f"/?d={y.isoformat()}").text.split('id="extra-doses"')[1].split("</section>")[0])
    assert "Filgrastim" in extra and "3:02 PM" in extra and "Not on the schedule this day" in extra
    r = client.post("/dose/new", data={"medication_id": plan["medication_id"], "taken_at": f"{y.isoformat()}T18:00",
                                       "back": "/"})
    assert "Check before you log this" in r.text
    with session(cfg.db_path) as conn:
        assert len(_taken(conn, "Filgrastim", y)) == 1
        report = export.dose_rows(conn, y, y, local_now(cfg))
        assert [r.status for r in report if r.medicine.startswith("Filgrastim")] == ["Taken"]


def test_ok_scheduled_to_clinic_and_stopping_a_medicine_keep_the_dose(client, cfg):
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))
    y = t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        fil, dex = _plan(conn, "Filgrastim"), _plan(conn, "Dexamethasone")
    for p, hhmm in ((fil, "15:00"), (dex, "08:00")):
        client.post("/slot", data={"plan_id": p["id"], "slot": f"{y.isoformat()} {hhmm}", "status": "taken",
                                   "taken_at": f"{y.isoformat()}T{hhmm}", "back": "/"}, follow_redirects=False)
    client.post(f"/plans/{fil['id']}", data={"kind": "clinic", "start_day": "2", "end_day": "7", "from_cycle": "1"},
                follow_redirects=False)
    client.post(f"/medicines/{dex['medication_id']}/active", data={"active": "0"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        clinic = _slots(conn, y, "Filgrastim")
        assert [s.kind for s in clinic] == ["clinic"] and clinic[0].log is not None
        _, rows, _ = schedule.grid(conn, schedule.cycle_for_date(conn, y), local_now(cfg))
        cell = next(r for r in rows if r.name == "Filgrastim").cells[2]
        assert cell.missing == 0 and len(cell.taken) == 1
    extra = client.get(f"/?d={y.isoformat()}").text.split('id="extra-doses"')[1].split("</section>")[0]
    assert "Dexamethasone" in extra


def test_ok_moving_day1_keeps_doses_on_their_calendar_dates(client, cfg):
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))  # yesterday = Day 2
    y = t - timedelta(days=1)
    with session(cfg.db_path) as conn:
        dex = _plan(conn, "Dexamethasone")
        c1 = conn.execute("SELECT * FROM cycles WHERE number = 1").fetchone()
    client.post("/slot", data={"plan_id": dex["id"], "slot": f"{y.isoformat()} 08:00", "status": "taken",
                               "taken_at": f"{y.isoformat()}T08:05", "back": "/"}, follow_redirects=False)
    for shift in (-1, 1):  # Day 1 entered a day late and corrected, then put back
        new_day1 = schedule.day1_of(c1) + timedelta(days=shift if shift < 0 else 0)
        r = client.post(f"/cycles/{c1['id']}", data={"number": "1", "day1": new_day1.isoformat(), "length_days": "14"},
                        follow_redirects=False)
        assert r.status_code == 303
        with session(cfg.db_path) as conn:
            s = _slots(conn, y, "Dexamethasone")
            assert len(s) == 1 and s[0].log is not None and s[0].log["taken_at"] == f"{y.isoformat()} 08:05"


# ---------------------------------------------------------------- F15: "Log a dose" fills a dose that isn't due yet


def test_F15_a_late_dose_from_log_a_dose_fills_the_missed_slot_not_the_coming_one(client, cfg):
    """Dexamethasone 8 AM and 8 PM. The morning dose is given late, at 3 PM, and logged with "Log a dose". _pair_logs
    picks the nearest open slot by clock distance, so it fills 8 PM (5 h away, not due yet) instead of 8 AM (7 h ago).
    The evening row then says "Taken 3:00 PM": no reminder, no warning, the evening dose is skipped. Filling 8 AM
    instead fails safe: logging the evening dose at 8 PM would still ask first (5 h after the 3 PM dose)."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))  # yesterday = Day 2
    y = t - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        plan = _plan(conn, "Dexamethasone")
        conn.execute("UPDATE plans SET times = '08:00,20:00' WHERE id = ?", (plan["id"],))
    r = client.post("/dose/new", data={"medication_id": plan["medication_id"], "taken_at": f"{y.isoformat()}T15:00",
                                       "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        by_time = {s.time: s.log for s in _slots(conn, y, "Dexamethasone")}
    assert by_time["20:00"] is None, "the late 3 PM dose was counted as the 8 PM dose, which then never comes due"



def test_F7b_doses_before_the_first_cycle_still_show_after_chemo_is_postponed(client, cfg):
    """Cycle 1's chemo is postponed a week on Day 1, after a "Took one" that morning. The day now falls before the
    first cycle's Day 0, extra_doses() returns [] without a cycle, and the dose appears nowhere on Today."""
    t = today(cfg)
    y = t - timedelta(days=1)
    setup_walkthrough(client, y)  # yesterday was planned as Day 1 (Prochlorperazine allowed as needed Days 1-6)
    with session(cfg.db_path) as conn:
        prn = _plan(conn, "Prochlorperazine", "as_needed")
        c1 = conn.execute("SELECT * FROM cycles WHERE number = 1").fetchone()
    r = client.post("/dose/quick", data={"medication_id": prn["medication_id"], "plan_id": prn["id"],
                                         "taken_at": f"{y.isoformat()}T07:00", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    assert "7:00 AM" in html.unescape(client.get(f"/?d={y.isoformat()}").text)  # shown before the change
    r = client.post(f"/cycles/{c1['id']}", data={"number": "1", "day1": (y + timedelta(days=7)).isoformat(),
                                                "length_days": "14"}, follow_redirects=False)
    assert r.status_code == 303
    assert "7:00 AM" in html.unescape(client.get(f"/?d={y.isoformat()}").text), \
        "after postponing Day 1, the Prochlorperazine taken at 7:00 AM is shown nowhere on that day"



# ---------------------------------------------------------------- F16: an as-needed plan switches off the planned check


def test_F16_a_scheduled_dose_right_after_an_as_needed_one_asks_first(client, cfg):
    """Prochlorperazine scheduled at 8 AM on Day 2 and also allowed as needed, with no time-between-doses saved.
    Jane gives one as needed at 7:30 AM; at 8:05 John gives the 8 AM dose and taps Taken. dose_warning returns None
    as soon as an as-needed plan covers the day, so the planned-dose check that would catch 35 min never runs."""
    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=2))  # yesterday = Day 2
    y = t - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        conn.execute("UPDATE medications SET min_hours_between = NULL WHERE name = 'Prochlorperazine'")
        prn = _plan(conn, "Prochlorperazine", "as_needed")
    r = client.post(f"/medicines/{prn['medication_id']}/plans", data={"kind": "scheduled", "start_day": "2",
                                                                       "end_day": "2", "times": "08:00",
                                                                       "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/dose/quick", data={"medication_id": prn["medication_id"], "plan_id": prn["id"],
                                         "taken_at": f"{y.isoformat()}T07:30", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        slot = _slots(conn, y, "Prochlorperazine")[0]
        assert slot.time == "08:00" and slot.log is None  # by design the as-needed dose isn't the scheduled one
    r = client.post("/slot", data={"plan_id": slot.plan["id"], "slot": slot.key, "status": "taken",
                                   "taken_at": f"{y.isoformat()}T08:05", "back": "/"}, follow_redirects=False)
    assert r.status_code == 200 and "Check before you log this" in r.text, \
        f"second Prochlorperazine 35 min after the first was logged without a warning (status {r.status_code})"
