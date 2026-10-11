"""Audit of phone reminders (ntfy): prep, due, "Not marked yet" and "Chemo tomorrow".

A reminder that says a dose is due or unmarked after it was given can cause a double dose; a missing
reminder can cause a missed dose. Every test calls `reminders.due_reminders` (or one
`ReminderScheduler.tick` with a fake sender) at fixed local times. Nothing is sent anywhere.

test_bug_* reproduce a finding and FAIL on the audited code (see audit-findings/reminders.md).
test_fine_* document cases that already behave and PASS.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from chemotracker import reminders, schedule, store
from chemotracker.db import get_settings, put_settings, session

# Made-up cycles. Cycle 1 Day 1 = Mon Mar 2 2026, 14 days. Cycle 2 Day 1 = Mar 16, so Sun Mar 15 is
# cycle 1 Day 14 AND cycle 2 Day 0. Day 2 = Mar 3, Day 3 = Mar 4.
DAY1 = date(2026, 3, 2)
D2, D3 = DAY1 + timedelta(days=1), DAY1 + timedelta(days=2)
LAST = DAY1 + timedelta(days=13)  # Mar 15


def at(d: date, hh: int, mm: int = 0, fold: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, fold=fold)


def k(kind: str, when: datetime) -> str:
    return f"{kind}|{when:%Y-%m-%d %H:%M}"


@pytest.fixture()
def db(cfg, monkeypatch):
    monkeypatch.setattr(schedule, "_TZ", cfg.tz)  # create_app sets this; the live scheduler runs with it
    with session(cfg.db_path, write=True) as conn:
        store.create_cycle(conn, {"number": "1", "day1": DAY1.isoformat(), "length_days": "14"})
        store.create_cycle(conn, {"number": "2", "day1": (DAY1 + timedelta(days=14)).isoformat(), "length_days": "14"})
        put_settings(conn, {"ntfy_enabled": "1", "ntfy_topic": "chemotracker-test", "app_url": "http://192.0.2.10:8000"})
    return cfg


def add_med(cfg, key=None, *, start, end, times=(), kind="scheduled", label=None, name=None, dose="1 tablet",
            prep=None, from_cycle=None, to_cycle=None, plan_dose=None):
    """A medicine whose ONLY plan is the one given (a catalog medicine's starter plan is removed)."""
    with session(cfg.db_path, write=True) as conn:
        if key:
            mid = store.add_from_catalog(conn, key)
        else:
            extra = {"prep_minutes": str(prep[0]), "prep_note": prep[1]} if prep else {}
            mid = store.create_medication(conn, {"name": name, "dose": dose, **extra})
        for p in store.plans(conn, mid):
            store.delete_plan(conn, p["id"])
        add_plan(conn, mid, start=start, end=end, times=times, kind=kind, label=label, from_cycle=from_cycle,
                 to_cycle=to_cycle, dose=plan_dose)
    return mid


def add_plan(conn, mid, *, start, end, times=(), kind="scheduled", label=None, from_cycle=None, to_cycle=None, dose=None):
    return store.create_plan(conn, mid, {
        "kind": kind, "start_day": str(start), "end_day": None if end is None else str(end), "times": list(times),
        "label": label, "dose": dose, "from_cycle": None if from_cycle is None else str(from_cycle),
        "to_cycle": None if to_cycle is None else str(to_cycle)})


def log_row(cfg, mid, slot: datetime, taken: datetime | None, status="taken", plan_index=0):
    """Taken / Skipped on the dose's own row (exact plan and planned time), like Today's buttons."""
    with session(cfg.db_path, write=True) as conn:
        store.log_slot(conn, store.plans(conn, mid)[plan_index], f"{slot:%Y-%m-%d %H:%M}", status, taken, None)


def log_a_dose(cfg, mid, taken: datetime):
    """What /dose/new ("Log a dose", "Took one earlier?") writes: no plan, no planned time."""
    with session(cfg.db_path, write=True) as conn:
        store.log_dose(conn, mid, taken, None, None)


def due(cfg, now, **prefs):
    with session(cfg.db_path) as conn:
        return reminders.due_reminders(conn, now, {**get_settings(conn), **prefs})


def keys(rs) -> list[str]:
    return [r.key for r in rs]


def tick(cfg, now) -> list[reminders.Reminder]:
    """One scheduler pass at local time `now` with a fake sender; returns what went out, in order."""
    nudger = reminders.ReminderScheduler(cfg)
    nudger.now = lambda: now
    sent: list[reminders.Reminder] = []
    nudger.tick(lambda prefs, r: sent.append(r))
    return sent


def sweep(cfg, start: datetime, end: datetime, step=5) -> list[str]:
    """Run the scheduler every `step` minutes from start to end; every key that went out."""
    out, t = [], start
    while t <= end:
        out += keys(tick(cfg, t))
        t += timedelta(minutes=step)
    return out


def today_slots(cfg, d, now):
    with session(cfg.db_path) as conn:
        return schedule.day_plan(conn, d, now).scheduled


# ============================================================================ reproductions (fail today)


def test_bug_late_bedtime_dose_logged_after_midnight_closes_tonights_dose(db):
    """F1. Olanzapine at bedtime (9 PM), days 2-4. Day 2's dose is given late, at 12:30 AM, and logged from
    "Log a dose". The pairing prefers ANY open slot on the new day over last night's, so tonight's 9 PM dose
    shows Taken 12:30 AM and no 9 PM reminder or nag goes out."""
    mid = add_med(db, "olanzapine", start=2, end=4, times=["21:00"], label="At bedtime")
    log_a_dose(db, mid, at(D3, 0, 30))  # last night's (Day 2) 9 PM dose, given late
    tonight = at(D3, 21, 0)
    shown = [s.status_text(tonight) for s in today_slots(db, D3, tonight) if s.plan["medication_id"] == mid]
    sent = sweep(db, at(D3, 20, 0), at(D3, 22, 30))
    assert k("due", tonight) in sent and k("nag", tonight) in sent, (
        f"tonight's 9 PM dose was never reminded; Today shows it as {shown}; sent={sent}")


def test_bug_late_morning_dose_from_log_a_dose_closes_the_evening_dose(db):
    """F2. Dexamethasone 8 AM and 8 PM. The morning dose is forgotten, given at 2:30 PM and logged from
    "Log a dose". 2:30 PM is nearer 8 PM than 8 AM, so the log closes the 8 PM dose: no 8 PM reminder."""
    mid = add_med(db, "dexamethasone", start=2, end=4, times=["08:00", "20:00"])
    log_a_dose(db, mid, at(D2, 14, 30))
    evening = at(D2, 20, 0)
    shown = {s.time: s.status_text(evening) for s in today_slots(db, D2, evening) if s.plan["medication_id"] == mid}
    sent = sweep(db, at(D2, 19, 0), at(D2, 21, 30))
    assert k("due", evening) in sent, f"no 8 PM reminder; Today shows {shown}; sent={sent}"


def test_bug_dose_given_just_before_midnight_is_reminded_again_at_midnight(db):
    """F3. Ondansetron every 8 hours (12 AM, 8 AM, 4 PM), days 2-4. Day 2's doses are marked on their rows.
    The 12 AM dose is given at 11:50 PM, before bed, and logged from "Log a dose" (its row is tomorrow's).
    A dose logged at 11:50 PM is only ever matched to that same day, so at midnight the reminder says the
    dose that was given 10 minutes ago is due, then "Not marked yet"."""
    mid = add_med(db, "ondansetron", start=2, end=4, times=["00:00", "08:00", "16:00"])
    for hh in (0, 8, 16):
        log_row(db, mid, at(D2, hh), at(D2, hh, 5))
    with session(db.db_path) as conn:  # through /dose/new this warning shows first; "Log it anyway" writes it
        warning = schedule.dose_warning(conn, store.medication(conn, mid), at(D2, 23, 50), now=at(D2, 23, 50))
    assert warning and "already logged" in warning
    log_a_dose(db, mid, at(D2, 23, 50))
    midnight = at(D3, 0, 0)
    sent = sweep(db, at(D2, 23, 50), at(D3, 1, 15))
    assert [x for x in sent if x.endswith(f"{midnight:%Y-%m-%d %H:%M}")] == [], sent


def test_bug_reminded_dose_on_a_cycles_last_day_is_not_on_today(db):
    """F4. An every-day medicine (Day 1 to the end of the cycle) at 8 AM. Mar 15 is cycle 1 Day 14 and also
    cycle 2 Day 0. Reminders look at both cycles and remind about cycle 1's Day 14 dose; Today only looks at
    cycle 2 (Day 0) and doesn't list the dose at all, so it can't be marked from a row and the nag follows."""
    mid = add_med(db, name="Acyclovir", start=1, end=None, times=["08:00"])
    now = at(LAST, 8, 0)
    assert k("due", now) in keys(due(db, now))  # the reminder goes out (current behaviour)
    shown = [s for s in today_slots(db, LAST, now) if s.plan["medication_id"] == mid]
    assert shown, "Today on Mar 15 doesn't show the 8 AM Acyclovir dose the reminder is about"


def test_bug_same_dose_in_two_overlapping_cycles_is_listed_twice(db):
    """F5a. A medicine planned from Day 0 to the end of the cycle. Mar 15 is in both cycle 1 (Day 14) and
    cycle 2 (Day 0), so the same 8 AM dose (same plan, same time) is found twice and the due reminder reads
    like two doses: '8:00 AM medicines: Example shot 1 syringe / Example shot 1 syringe'."""
    add_med(db, name="Example shot", dose="1 syringe", start=0, end=None, times=["08:00"],
            prep=(30, "Take it out of the fridge"))
    [r] = [r for r in due(db, at(LAST, 8, 0)) if r.key.startswith("due|")]
    assert r.message.count("Example shot") == 1, f"{r.title!r}: {r.message!r}"


def test_bug_same_prep_reminder_is_sent_twice_in_one_pass(db):
    """F5b. Same set-up: the prep reminder appears twice with one key, and one scheduler pass sends both."""
    add_med(db, name="Example shot", dose="1 syringe", start=0, end=None, times=["08:00"],
            prep=(30, "Take it out of the fridge"))
    sent = keys(tick(db, at(LAST, 7, 30)))
    assert len(sent) == len(set(sent)), sent


def test_bug_dose_planned_past_the_cycle_end_is_never_reminded(db):
    """F6. A 7-day course on days 10-16 of cycle 1 only (14-day cycle). Days 15 and 16 fall on cycle 2's
    Days 1 and 2. `_slots_on` only looks back one cycle on the next cycle's Day 0, so these planned doses
    get no due reminder and no nag (and Today doesn't show them either)."""
    add_med(db, name="Example antibiotic", start=10, end=16, times=["08:00"], from_cycle=1, to_cycle=1)
    with session(db.db_path) as conn:  # the plan really is in cycle 1's schedule on Mar 16 and Mar 17
        c1 = schedule.cycle_for_date(conn, DAY1)
        planned = [s.key for s in schedule.slots(conn, c1) if s.day in (15, 16)]
    assert planned == ["2026-03-16 08:00", "2026-03-17 08:00"]
    day15 = at(DAY1 + timedelta(days=14), 8, 0)
    on_today = [s.key for s in today_slots(db, day15.date(), day15)]
    sent = sweep(db, day15 - timedelta(minutes=30), day15 + timedelta(minutes=75))
    assert k("due", day15) in sent, f"no reminder for a planned dose; Today lists {on_today}; sent={sent}"


def test_bug_prep_reminder_for_a_dose_at_midnight_never_goes_out(db):
    """F7. Filgrastim (out of the fridge 30 minutes before) at 12:00 AM. The prep reminder belongs at
    11:30 PM the evening before, but only yesterday's and today's doses are looked at, and at midnight
    it is already too late (prep only goes out before the dose time)."""
    add_med(db, "filgrastim", start=2, end=7, times=["00:00"])
    shot = at(D2, 0, 0)
    sent = sweep(db, shot - timedelta(minutes=40), shot + timedelta(minutes=10), step=1)
    assert any(x.startswith("prep|") for x in sent), sent


def test_bug_scheduled_dose_logged_minutes_early_on_an_as_needed_day_reminds_as_if_not_given(db):
    """F8. Olanzapine at bedtime (9 PM) days 1-4 that is also allowed as needed days 1-6. It is given at
    8:55 PM and logged from "Log a dose". The log is deliberately left unpaired (it might be the as-needed
    one), so at 9 PM 'Olanzapine at 9:00 PM' and at 9:30 PM 'Not marked yet' go out with no word that a
    dose was logged five minutes earlier. Whoever reads the phone may give a second dose."""
    mid = add_med(db, "olanzapine", start=1, end=4, times=["21:00"], label="At bedtime")
    with session(db.db_path, write=True) as conn:
        add_plan(conn, mid, kind="as_needed", start=1, end=6)
    log_a_dose(db, mid, at(D2, 20, 55))
    nine = at(D2, 21, 0)
    out = [r for t in (nine, nine + timedelta(minutes=30)) for r in tick(db, t)]
    assert out, "expected the due and nag reminders to exist (current behaviour)"
    assert all("8:55" in r.message for r in out), [(r.title, r.message) for r in out]


def test_bug_dose_moved_onto_a_time_already_reminded_is_never_reminded(db):
    """F9. Dexamethasone at 8 AM, Filgrastim at 9 AM. The 8 AM reminder goes out. At 8:05 Filgrastim's
    time is changed to 8 AM. With 'Remind again if not marked' Off, Filgrastim never gets a reminder,
    because the due key is per clock time ('due|<date> 08:00') and was already used."""
    add_med(db, "dexamethasone", start=2, end=4, times=["08:00"])
    fil = add_med(db, "filgrastim", start=2, end=7, times=["09:00"])
    with session(db.db_path, write=True) as conn:
        put_settings(conn, {"ntfy_nag": "0"})
    eight = at(D2, 8, 0)
    assert keys(tick(db, eight)) == [k("due", eight)]
    with session(db.db_path, write=True) as conn:
        p = store.plans(conn, fil)[0]
        store.update_plan(conn, p["id"], {"kind": "scheduled", "start_day": "2", "end_day": "7", "times": ["08:00"]})
    later = [r for t in range(5, 50, 5) for r in tick(db, eight + timedelta(minutes=t))]
    assert any("Filgrastim" in r.title + r.message for r in later), [r.key for r in later]


def test_bug_not_marked_yet_waits_90_real_minutes_on_the_fall_back_night(db):
    """F10. Sun Nov 1 2026, America/Los_Angeles: 1:59 AM PDT is followed by 1:00 AM PST. A 1:30 AM dose
    (first 1:30) gets its due reminder, but 'Not marked yet' compares wall-clock times, so it waits for
    the clock to read 2:00 AM, which is 90 real minutes after the dose instead of 30."""
    with session(db.db_path, write=True) as conn:
        store.create_cycle(conn, {"number": "3", "day1": "2026-10-26", "length_days": "14"})
    add_med(db, name="Example tablet", start=7, end=7, times=["01:30"])  # Day 7 = Sun Nov 1
    dose = datetime(2026, 11, 1, 1, 30)
    assert keys(tick(db, dose)) == ["due|2026-11-01 01:30"]
    later = schedule.real_add(dose, timedelta(minutes=30))  # 30 real minutes later
    assert (later.hour, later.minute, later.fold) == (1, 0, 1)
    assert "nag|2026-11-01 01:30" in keys(due(db, later)), "the nag waits until the clock reads 2:00 AM again"


# ============================================================================ checked and fine (pass today)


def test_fine_dose_given_early_on_its_row_or_from_log_a_dose_stops_prep_due_and_nag(db):
    fil = add_med(db, "filgrastim", start=2, end=7, times=["15:00"])
    log_row(db, fil, at(D2, 15), at(D2, 14, 0))  # Day 2 marked on its row an hour early
    log_a_dose(db, fil, at(D3, 14, 0))  # Day 3 logged from "Log a dose" an hour early
    for d in (D2, D3):
        assert sweep(db, at(d, 14, 0), at(d, 16, 30)) == []


def test_fine_skipped_dose_gets_no_reminders(db):
    fil = add_med(db, "filgrastim", start=2, end=7, times=["15:00"])
    log_row(db, fil, at(D2, 15), None, status="skipped")
    assert sweep(db, at(D2, 14, 0), at(D2, 16, 30)) == []


def test_fine_time_changed_after_the_dose_was_given_sends_nothing_new(db):
    fil = add_med(db, "filgrastim", start=2, end=7, times=["15:00"])
    assert keys(tick(db, at(D2, 15, 0))) == [k("due", at(D2, 15))]
    log_row(db, fil, at(D2, 15), at(D2, 15, 5))
    with session(db.db_path, write=True) as conn:
        p = store.plans(conn, fil)[0]
        store.update_plan(conn, p["id"], {"kind": "scheduled", "start_day": "2", "end_day": "7", "times": ["17:00"]})
    assert sweep(db, at(D2, 15, 10), at(D2, 18, 30)) == []


def test_fine_time_changed_before_the_reminder_moves_prep_and_due(db):
    fil = add_med(db, "filgrastim", start=2, end=7, times=["15:00"])
    with session(db.db_path, write=True) as conn:
        p = store.plans(conn, fil)[0]
        store.update_plan(conn, p["id"], {"kind": "scheduled", "start_day": "2", "end_day": "7", "times": ["16:00"]})
    sent = sweep(db, at(D2, 14, 0), at(D2, 16, 0))
    assert [x.split("|")[0] for x in sent] == ["prep", "due"] and sent[1] == k("due", at(D2, 16))


def test_fine_prep_minutes_changed_after_the_prep_reminder_is_not_repeated(db):
    fil = add_med(db, "filgrastim", start=2, end=7, times=["15:00"])
    assert [x.split("|")[0] for x in keys(tick(db, at(D2, 14, 30)))] == ["prep"]
    with session(db.db_path, write=True) as conn:
        store.update_medication(conn, fil, {"prep_minutes": "20", "prep_note": "Take it out of the fridge"})
    assert keys(tick(db, at(D2, 14, 40))) == []


def test_fine_nag_stops_once_the_dose_is_marked_in_the_nag_window(db):
    fil = add_med(db, "filgrastim", start=2, end=7, times=["15:00"])
    assert k("nag", at(D2, 15)) in keys(due(db, at(D2, 15, 30)))
    log_row(db, fil, at(D2, 15), at(D2, 15, 2))
    assert due(db, at(D2, 15, 36)) == []


def test_fine_bedtime_dose_nag_after_midnight_and_marking_it_from_last_nights_row(db):
    mid = add_med(db, "olanzapine", start=2, end=4, times=["23:30"], label="At bedtime")
    assert k("nag", at(D2, 23, 30)) in keys(due(db, at(D3, 0, 0)))  # yesterday's slots are scanned
    log_row(db, mid, at(D2, 23, 30), at(D3, 0, 10))  # "From last night" on Today
    assert due(db, at(D3, 0, 15)) == []


def test_fine_clinic_and_deactivated_medicines_get_no_dose_reminders(db):
    add_med(db, "doxorubicin", start=1, end=1, kind="clinic")
    fil = add_med(db, "filgrastim", start=2, end=7, times=["15:00"])
    with session(db.db_path, write=True) as conn:
        store.set_active(conn, fil, False)
    sent = sweep(db, at(DAY1, 0, 0), at(D2, 23, 55), step=15)
    assert [x for x in sent if not x.startswith("chemo|")] == []


def test_fine_catch_up_drops_stale_due_and_prep(db):
    add_med(db, "filgrastim", start=2, end=7, times=["15:00"])
    assert keys(due(db, at(D2, 15, 50))) == [k("nag", at(D2, 15))]  # server back late: no stale "due"
    assert due(db, at(D2, 18, 0)) == []


def test_fine_two_plans_of_one_medicine_at_the_same_time(db):
    mid = add_med(db, "dexamethasone", start=2, end=4, times=["08:00"], plan_dose="2 tablets")
    with session(db.db_path, write=True) as conn:
        add_plan(conn, mid, start=2, end=4, times=["08:00"], dose="1 tablet")
    log_row(db, mid, at(D2, 8), at(D2, 7, 55), plan_index=0)
    [r] = due(db, at(D2, 8, 0))
    assert r.message.count("Dexamethasone") == 1 and "1 tablet" in r.message


def test_fine_day_0_dose_is_reminded_and_on_today(db):
    mid = add_med(db, name="Example premed", start=0, end=0, times=["20:00"])
    now = at(LAST, 20, 0)
    assert k("due", now) in keys(due(db, now))
    assert [s.plan["medication_id"] for s in today_slots(db, LAST, now)] == [mid]


def test_fine_chemo_tomorrow_follows_a_moved_day_1(db):
    assert k("chemo", at(DAY1 + timedelta(days=14), 0))[:16] in [x[:16] for x in keys(tick(db, at(LAST, 18, 0)))]
    with session(db.db_path, write=True) as conn:
        c2 = conn.execute("SELECT id FROM cycles WHERE number = 2").fetchone()["id"]
        store.update_cycle(conn, c2, {"number": "2", "day1": "2026-03-23", "length_days": "14"})
    assert keys(tick(db, at(LAST, 18, 30))) == []
    assert keys(tick(db, at(date(2026, 3, 22), 18, 0))) == ["chemo|2026-03-23"]


def test_fine_fall_back_night_does_not_repeat_the_1_30_reminder(db):
    with session(db.db_path, write=True) as conn:
        store.create_cycle(conn, {"number": "3", "day1": "2026-10-26", "length_days": "14"})
    add_med(db, name="Example tablet", start=7, end=7, times=["01:30"])
    assert keys(tick(db, datetime(2026, 11, 1, 1, 30))) == ["due|2026-11-01 01:30"]
    # Windows are real time now (F10): "Not marked yet" goes out 30 real minutes later, at 1:00 AM PST. The
    # scheduler ticks every 30 seconds, so that pass happens before the second 1:30 comes round.
    assert keys(tick(db, datetime(2026, 11, 1, 1, 0, fold=1))) == ["nag|2026-11-01 01:30"]
    assert keys(tick(db, datetime(2026, 11, 1, 1, 30, fold=1))) == []  # the second 1:30 AM (PST)


# ============================================================================ F1 and F2 through the real route


def _walkthrough_med(client, cfg, name):
    from .conftest import setup_walkthrough, today

    t = today(cfg)
    setup_walkthrough(client, t - timedelta(days=3))  # Day 2 = t-2, Day 3 = t-1: fixed past days, any clock
    with session(cfg.db_path) as conn:
        mid = conn.execute("SELECT id FROM medications WHERE name = ?", (name,)).fetchone()["id"]
    return mid, t - timedelta(days=1)


def test_bug_route_log_a_dose_after_midnight_closes_tonights_bedtime_dose_without_a_warning(client, cfg):
    """F1 via POST /dose/new with the walkthrough's own plan (Olanzapine 'At bedtime' 9 PM, days 2-4)."""
    olz, d3 = _walkthrough_med(client, cfg, "Olanzapine")
    r = client.post("/dose/new", data={"medication_id": str(olz), "taken_at": f"{d3.isoformat()}T00:30", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303, "expected no warning page: " + r.text[:300]
    tonight = at(d3, 21, 0)
    shown = [s.status_text(tonight) for s in today_slots(cfg, d3, tonight) if s.plan["medication_id"] == olz]
    assert k("due", tonight) in keys(due(cfg, tonight)), f"Today shows Day 3's 9 PM dose as {shown}"


def test_bug_route_late_morning_dose_from_log_a_dose_closes_the_evening_dose_without_a_warning(client, cfg):
    """F2 via POST /dose/new: the walkthrough's Dexamethasone made twice a day (8 AM, 8 PM)."""
    dex, d3 = _walkthrough_med(client, cfg, "Dexamethasone")
    with session(cfg.db_path, write=True) as conn:
        p = store.plans(conn, dex)[0]
        store.update_plan(conn, p["id"], {"kind": "scheduled", "start_day": "2", "end_day": "4", "times": ["08:00", "20:00"]})
    r = client.post("/dose/new", data={"medication_id": str(dex), "taken_at": f"{d3.isoformat()}T14:30", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303, "expected no warning page: " + r.text[:300]
    evening = at(d3, 20, 0)
    shown = {s.time: s.status_text(evening) for s in today_slots(cfg, d3, evening) if s.plan["medication_id"] == dex}
    assert k("due", evening) in keys(due(cfg, evening)), f"Today shows {shown}"


# ============================================================================ more reproductions


def test_bug_home_dose_from_log_a_dose_is_taken_by_the_unlogged_clinic_dose_of_the_same_medicine(db):
    """F11. Dexamethasone is given at the clinic on Day 1 (premed, not logged by the family) and taken at home
    at 8 PM on Day 1. The home dose is given at 7:50 PM and logged from "Log a dose". A clinic dose has no
    time, so its gap counts as 0 and it always wins the pairing: the log closes the CLINIC dose, the 8 PM home
    dose stays open, and the phone says it's due at 8 PM and 'Not marked yet' at 8:30 PM."""
    mid = add_med(db, "dexamethasone", start=1, end=1, kind="clinic")
    with session(db.db_path, write=True) as conn:
        add_plan(conn, mid, start=1, end=1, times=["20:00"])
    log_a_dose(db, mid, at(DAY1, 19, 50))
    eight = at(DAY1, 20, 0)
    with session(db.db_path) as conn:
        plan = schedule.day_plan(conn, DAY1, eight)
        shown = [(s.kind, s.status_text(eight)) for s in plan.clinic + plan.scheduled]
    sent = sweep(db, at(DAY1, 19, 50), at(DAY1, 21, 0))
    assert sent == [], f"Today shows {shown}; sent={sent}"


def test_bug_spring_forward_dose_between_2_and_3_am_never_gets_its_due_reminder(db):
    """F10b. Sun Mar 8 2026 (cycle 1 Day 7): clocks jump from 1:59 AM to 3:00 AM. A 2:15 AM dose's due window
    (2:15-3:00 AM wall clock) never happens; the first notice is 'Not marked yet' at 3:00 AM."""
    add_med(db, name="Example tablet", start=7, end=7, times=["02:15"])
    spring = DAY1 + timedelta(days=6)
    sent = sweep(db, at(spring, 1, 0), at(spring, 1, 59), step=1) + sweep(db, at(spring, 3, 0), at(spring, 4, 0), step=1)
    assert k("due", at(spring, 2, 15)) in sent, sent


def test_bug_restoring_a_backup_resends_reminders_that_already_went_out(db):
    """F12. Restoring a backup replaces the whole database, `reminders_sent` included. A due reminder that went
    out at 3:00 PM goes out again after a restore at 3:10 PM (anything still inside its 45-minute window does),
    although nothing about the dose changed. Had the dose been logged at 3:05 PM, the restore would also drop
    that log and the repeat would say a given dose is due."""
    from chemotracker import backup

    add_med(db, "filgrastim", start=2, end=7, times=["15:00"])
    db.backup_dir.mkdir(parents=True, exist_ok=True)
    nightly = backup.free_name(db.backup_dir, f"{backup.PREFIX}20260303-030000-nightly")
    backup.snapshot(db, nightly)  # the 3 AM backup
    three = at(D2, 15, 0)
    assert keys(tick(db, three)) == [k("due", three)]
    backup.restore(db, nightly, None)  # e.g. to undo a medicine deleted by mistake
    assert keys(tick(db, three + timedelta(minutes=10))) == []


def test_bug_fall_back_prep_reminder_comes_two_real_hours_early(db):
    """F10c. Nov 1 2026: a 2:30 AM dose with a 60-minute prep step. Its prep time is computed in real time
    (1:30 AM PST, the SECOND 1:30), but the window compares wall-clock times, so it already goes out at the
    FIRST 1:30 AM (PDT): two real hours before the dose instead of one."""
    with session(db.db_path, write=True) as conn:
        store.create_cycle(conn, {"number": "3", "day1": "2026-10-26", "length_days": "14"})
    add_med(db, name="Example shot", dose="1 syringe", start=7, end=7, times=["02:30"], prep=(60, "Take it out of the fridge"))
    first_130 = datetime(2026, 11, 1, 1, 30)  # PDT, 2 real hours before 2:30 AM PST
    assert schedule.real_diff(datetime(2026, 11, 1, 2, 30), first_130) == timedelta(hours=2)
    assert [r.key for r in due(db, first_130) if r.key.startswith("prep|")] == [], "prep sent 2 real hours early"
