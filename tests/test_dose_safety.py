"""Double-dose safety: a logged dose must keep counting, and every way of logging checks what's already there.

The failure these guard against happened for real: a Filgrastim shot was logged, then its reminder time was
changed, and the app showed the shot as due again and let a second one be logged without a warning.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta

from chemotracker import export, reminders, schedule, store
from chemotracker.db import session

from .conftest import local_now, setup_walkthrough, today


def _plan(conn, med_name, kind="scheduled"):
    return conn.execute("""SELECT p.* FROM plans p JOIN medications m ON m.id = p.medication_id
                            WHERE m.name = ? AND p.kind = ?""", (med_name, kind)).fetchone()


def _slots(conn, cfg, d, med_name):
    cycle = schedule.cycle_for_date(conn, d)
    return [s for s in schedule.slots(conn, cycle, schedule.day_number(cycle, d)) if s.plan["med_name"] == med_name]


def _taken(conn, med_name, d):
    return conn.execute("""SELECT dl.* FROM dose_logs dl JOIN medications m ON m.id = dl.medication_id
                            WHERE m.name = ? AND dl.status = 'taken' AND substr(dl.taken_at, 1, 10) = ?""",
                        (med_name, d.isoformat())).fetchall()


def _set_times(client, conn, plan, times):
    """Change a schedule's times through the real form, the way the family did."""
    r = client.post(f"/plans/{plan['id']}", data={"kind": plan["kind"], "start_day": str(plan["start_day"]),
                                                  "end_day": str(plan["end_day"] or ""), "times": times,
                                                  "label": plan["label"] or "", "from_cycle": "1"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def test_changing_the_reminder_time_keeps_the_logged_dose(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # yesterday was Day 2: Filgrastim at 3:00 PM
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
        slot = _slots(conn, cfg, y, "Filgrastim")[0]
    r = client.post("/slot", data={"plan_id": plan["id"], "slot": slot.key, "status": "taken",
                                   "taken_at": f"{y.isoformat()}T12:57", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        r = _set_times(client, conn, plan, "12:30")
    with session(cfg.db_path) as conn:
        moved = _slots(conn, cfg, y, "Filgrastim")
        assert [s.time for s in moved] == ["12:30"]
        assert moved[0].log is not None and moved[0].log["taken_at"] == f"{y.isoformat()} 12:57"
        # Nothing downstream may think it's still owed: the cycle sheet, the report, or the reminders.
        cycle = schedule.cycle_for_date(conn, y)
        _, rows, _ = schedule.grid(conn, cycle, local_now(cfg))
        cell = next(r for r in rows if r.name == "Filgrastim").cells[2]
        assert cell.missing == 0 and len(cell.taken) == 1
        report = export.dose_rows(conn, y, y, local_now(cfg))
        assert [r.status for r in report if r.medicine.startswith("Filgrastim")] == ["Taken"]
        assert all(s.log is not None for s in reminders._slots_on(conn, y) if s.plan["med_name"] == "Filgrastim")
    page = client.get(f"/?d={y.isoformat()}").text
    row = page.split(">Filgrastim</a>")[1].split('class="dose-side"')[0]  # just this row's status
    assert "Taken 12:57 PM" in row and "Not logged" not in row
    # Tapping the moved row's Given/Taken again must stop and say it's already logged, without a new record.
    r = client.post("/slot", data={"plan_id": plan["id"], "slot": moved[0].key, "status": "taken",
                                   "taken_at": f"{y.isoformat()}T15:05", "back": "/"}, follow_redirects=False)
    assert r.status_code == 409 and "Already recorded" in r.text and "12:57 PM" in r.text
    with session(cfg.db_path) as conn:
        assert len(_taken(conn, "Filgrastim", y)) == 1
    # "Replace it with mine" corrects that one record. It never adds a second.
    client.post("/slot", data={"plan_id": plan["id"], "slot": moved[0].key, "status": "taken", "confirm": "1",
                               "taken_at": f"{y.isoformat()}T13:05", "back": "/"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        logs = _taken(conn, "Filgrastim", y)
        assert len(logs) == 1 and logs[0]["taken_at"] == f"{y.isoformat()} 13:05" and logs[0]["slot"] == moved[0].key


def test_saving_a_schedule_says_todays_dose_still_counts(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    with session(cfg.db_path, write=True) as conn:
        plan = _plan(conn, "Filgrastim")
        slot = _slots(conn, cfg, today(cfg), "Filgrastim")[0]
        earlier = local_now(cfg).replace(second=0, microsecond=0) - timedelta(minutes=1)
        store.log_slot(conn, plan, slot.key, "taken", earlier, None)
    with session(cfg.db_path) as conn:
        r = _set_times(client, conn, plan, "23:59")
    page = client.get(r.headers["location"]).text
    assert "still counts" in page and schedule.fmt_time(earlier) in page


def test_twice_a_day_doses_follow_the_new_times(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        plan = _plan(conn, "Dexamethasone")
        conn.execute("UPDATE plans SET times = '08:00,20:00' WHERE id = ?", (plan["id"],))
        plan = store.plan(conn, plan["id"])
        store.log_slot(conn, plan, f"{y.isoformat()} 08:00", "taken", datetime.combine(y, datetime.min.time()).replace(hour=8, minute=5), None)
        conn.execute("UPDATE plans SET times = '09:00,21:00' WHERE id = ?", (plan["id"],))
    with session(cfg.db_path) as conn:
        by_time = {s.time: s.log for s in _slots(conn, cfg, y, "Dexamethasone")}
    assert by_time["09:00"] is not None and by_time["21:00"] is None


def test_a_replaced_schedule_keeps_the_doses_logged_on_the_old_one(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        plan = _plan(conn, "Filgrastim")
        store.log_slot(conn, plan, f"{y.isoformat()} 15:00", "taken", datetime.combine(y, datetime.min.time()).replace(hour=14), None)
    client.post(f"/plans/{plan['id']}/delete", follow_redirects=False)
    client.post(f"/medicines/{plan['medication_id']}/plans", data={"kind": "scheduled", "start_day": "2", "end_day": "7",
                                                                    "times": "13:00", "from_cycle": "1"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        new = _slots(conn, cfg, y, "Filgrastim")
        # SQLite may give the new schedule the deleted one's id, so check the new time, not the id.
        assert len(new) == 1 and new[0].time == "13:00" and new[0].log is not None


def test_log_a_dose_counts_for_the_scheduled_row_and_the_row_then_refuses_a_second(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT id FROM medications WHERE name = 'Dexamethasone'").fetchone()["id"]
        plan = _plan(conn, "Dexamethasone")
    client.post("/dose/new", data={"medication_id": med, "taken_at": f"{y.isoformat()}T09:10", "back": "/"})
    with session(cfg.db_path) as conn:
        slot = _slots(conn, cfg, y, "Dexamethasone")[0]
        assert slot.log is not None  # "Log a dose" counts for the 8:00 AM row
    r = client.post("/slot", data={"plan_id": plan["id"], "slot": slot.key, "status": "taken", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 409 and "9:10 AM" in r.text
    # A second "Log a dose" the same day: planned once, already logged, so it asks first.
    r = client.post("/dose/new", data={"medication_id": med, "taken_at": f"{y.isoformat()}T18:00", "back": "/"})
    assert "Check before you log this" in r.text and "planned once" in r.text and "9:10 AM" in r.text
    with session(cfg.db_path) as conn:
        assert len(_taken(conn, "Dexamethasone", y)) == 1
    # Logged anyway (someone was sure): it shows, flagged, never hidden.
    client.post("/dose/new", data={"medication_id": med, "taken_at": f"{y.isoformat()}T18:00", "confirm": "1", "back": "/"})
    page = client.get(f"/?d={y.isoformat()}").text
    extra = page.split('id="extra-doses"')[1].split("</section>")[0]
    assert "Dexamethasone" in extra and "6:00 PM" in extra and "More than planned" in extra


def test_taken_on_a_second_daily_dose_too_soon_asks_first(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        plan = _plan(conn, "Dexamethasone")
        conn.execute("UPDATE plans SET times = '08:00,20:00' WHERE id = ?", (plan["id"],))
        plan = store.plan(conn, plan["id"])
        store.log_slot(conn, plan, f"{y.isoformat()} 08:00", "taken", datetime.combine(y, datetime.min.time()).replace(hour=8, minute=5), None)
    r = client.post("/slot", data={"plan_id": plan["id"], "slot": f"{y.isoformat()} 20:00", "status": "taken",
                                   "taken_at": f"{y.isoformat()}T08:30", "back": "/"}, follow_redirects=False)
    assert r.status_code == 200 and "Check before you log this" in r.text and "8:05 AM" in r.text
    assert "Leave it as it is" in r.text
    with session(cfg.db_path) as conn:
        assert len(_taken(conn, "Dexamethasone", y)) == 1
    # At the planned evening time it's simply logged.
    r = client.post("/slot", data={"plan_id": plan["id"], "slot": f"{y.isoformat()} 20:00", "status": "taken",
                                   "taken_at": f"{y.isoformat()}T20:02", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_took_all_leaves_out_a_dose_that_is_already_logged_elsewhere(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))  # yesterday was Day 3
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        dex = conn.execute("SELECT id FROM medications WHERE name = 'Dexamethasone'").fetchone()["id"]
        conn.execute("""INSERT INTO plans (medication_id, kind, start_day, end_day, times, from_cycle, created_at)
                        SELECT id, 'scheduled', 1, NULL, '08:00', 1, datetime('now') FROM medications WHERE name = 'Olanzapine'""")
        store.log_dose(conn, dex, datetime.combine(y, datetime.min.time()).replace(hour=7, minute=40), "2 tablets", None)
        items = [f"{s.plan['id']}|{s.key}" for s in _slots(conn, cfg, y, "Dexamethasone") + _slots(conn, cfg, y, "Olanzapine")
                 if s.time == "08:00"]
    r = client.post("/slots/taken", data={"slots[]": items, "when": "at", "at_time": "08:10", "back": "/"})
    assert "Olanzapine marked taken" in r.text
    with session(cfg.db_path) as conn:
        assert len(_taken(conn, "Dexamethasone", y)) == 1 and len(_taken(conn, "Olanzapine", y)) == 1


def test_a_scheduled_medicine_on_a_day_it_isnt_planned_asks_first(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=9))  # Day 10: Filgrastim is Days 2-7
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT id FROM medications WHERE name = 'Filgrastim'").fetchone()["id"]
    r = client.post("/dose/new", data={"medication_id": med, "ago": "0", "back": "/"})
    text = html.unescape(r.text)
    assert "Check before you log this" in text and "isn't planned today" in text and "Days 2" in text
    with session(cfg.db_path) as conn:
        assert _taken(conn, "Filgrastim", today(cfg)) == []


def test_as_needed_doses_never_fill_a_scheduled_row(client, cfg):
    # A medicine on both a schedule and as needed: a "Took one" is an extra dose, not the scheduled one.
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        med = conn.execute("SELECT id FROM medications WHERE name = 'Prochlorperazine'").fetchone()["id"]
        conn.execute("""INSERT INTO plans (medication_id, kind, start_day, end_day, times, from_cycle, created_at)
                        VALUES (?, 'scheduled', 2, 2, '08:00', 1, datetime('now'))""", (med,))
        store.log_dose(conn, med, datetime.combine(y, datetime.min.time()).replace(hour=9), "1 tablet", None)
    with session(cfg.db_path) as conn:
        assert _slots(conn, cfg, y, "Prochlorperazine")[0].log is None
