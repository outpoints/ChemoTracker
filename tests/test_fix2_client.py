"""Today, as-needed and reminder-engine leftovers from the safety audit (fix2/client).

Each test_bug_* reproduced its finding and failed before the fix. test_fine_* guard behaviour that was already right.
- S4: a medicine both planned and allowed as needed on one day. After "Took one", its planned row says so.
- S7: a small-hours dose on a day the medicine is also allowed as needed fills last night's bedtime dose.
- The early-dose warning names whoever last changed the entry, like the other dose warnings.
- S3: a crash in the reminder check shows in Settings in plain words, and clears after the next good check.
The browser-side items (the "Other time" clock, the countdown at zero, the countdown's rounding) are in
tests/browser/test_today_js.py.
"""

from __future__ import annotations

import html
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from chemotracker import reminders, schedule, store, web
from chemotracker.db import put_settings, session

from .conftest import setup_walkthrough, today

LA = ZoneInfo("America/Los_Angeles")

# Made-up cycle: Day 1 = Mon Mar 2 2026, so Day 4 = Thu Mar 5 and Day 5 = Fri Mar 6.
DAY1 = date(2026, 3, 2)
D4, D5 = DAY1 + timedelta(days=3), DAY1 + timedelta(days=4)


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm))


def freeze(monkeypatch, local: datetime) -> None:
    """Make web.now() return this local wall-clock time."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return local.replace(tzinfo=LA).astimezone(tz) if tz is not None else local

    monkeypatch.setattr(web, "datetime", Frozen)


def med_row(conn, name: str):
    return conn.execute("SELECT * FROM medications WHERE name = ?", (name,)).fetchone()


def plan_values(kind: str, start: int, end: int, times=(), label=None) -> dict:
    return {"kind": kind, "start_day": str(start), "end_day": str(end), "times": list(times), "label": label,
            "dose": None, "from_cycle": None, "to_cycle": None}


# ============================================================================ S4


def test_bug_took_one_shows_on_the_planned_row_of_the_same_medicine(client, cfg, monkeypatch):
    """S4. Prochlorperazine is allowed as needed on days 1-6 (6 hours apart) and also planned at 8:00 AM on days
    2-4. At 7:40 AM on Day 3 someone taps Took one. The 8:00 AM row read as a plain "Due now" with a Taken button,
    as if nothing had been given. It must say one was just taken, keep the dose and its Taken button, and Taken
    must still warn."""
    day1 = today(cfg) - timedelta(days=10)
    setup_walkthrough(client, day1)
    d3 = day1 + timedelta(days=2)
    with session(cfg.db_path, write=True) as conn:
        med = med_row(conn, "Prochlorperazine")
        prn = next(p for p in store.plans(conn, med["id"]) if p["kind"] == "as_needed")
        planned = store.create_plan(conn, med["id"], plan_values("scheduled", 2, 4, ["08:00"]))
    freeze(monkeypatch, at(d3, 7, 40))
    r = client.post("/dose/quick", data={"medication_id": med["id"], "plan_id": prn["id"], "ago": "0", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = html.unescape(client.get("/").text)
    row_id = f'id="slot-{planned}-{d3.isoformat()}-0800"'
    assert row_id in page, "the planned 8:00 AM dose must stay on Today"
    info = page.split(row_id)[1].split('class="dose-side"')[0]
    side = page.split(row_id)[1].split('class="dose-side"')[1].split("</form>")[0]
    assert "Took one at 7:40 AM. Check before giving this one." in info, info
    assert "Taken" in side and 'name="when" value="now"' in side, "the dose keeps its Taken button"
    # Taken still asks first.
    r = client.post("/slot", data={"plan_id": planned, "slot": f"{d3.isoformat()} 08:00", "medication_id": med["id"],
                                   "back": "/", "status": "taken", "when": "now"})
    text = html.unescape(r.text)
    assert "Leave it as it is" in text and "7:40 AM" in text, text[:2000]


def test_bug_planned_row_note_only_for_a_recent_dose_that_fills_no_planned_dose(client, cfg, monkeypatch):
    """S4. The note is only for a dose of the same medicine that fills no planned dose and is inside the label's gap.
    A past day shows it too, and it goes once the planned dose is logged."""
    day1 = today(cfg) - timedelta(days=10)
    setup_walkthrough(client, day1)
    d3, d4 = day1 + timedelta(days=2), day1 + timedelta(days=3)
    with session(cfg.db_path, write=True) as conn:
        med = med_row(conn, "Prochlorperazine")
        prn = next(p for p in store.plans(conn, med["id"]) if p["kind"] == "as_needed")
        store.create_plan(conn, med["id"], plan_values("scheduled", 2, 4, ["08:00"]))
        store.log_dose(conn, med["id"], at(d3, 1, 0), None, None, prn["id"])  # 7 hours before, past the 6 hour gap
    freeze(monkeypatch, at(d3, 8, 5))
    assert "Check before giving this one" not in html.unescape(client.get("/").text)
    with session(cfg.db_path, write=True) as conn:
        store.log_dose(conn, med["id"], at(d4, 7, 30), None, None, prn["id"])
    freeze(monkeypatch, at(d4 + timedelta(days=1), 9, 0))  # Day 4 is an earlier day now
    page = html.unescape(client.get(f"/?d={d4.isoformat()}").text)
    assert "Took one at 7:30 AM. Check before giving this one." in page  # an earlier day shows it too
    planned = next(p["id"] for p in store_plans(cfg, med["id"]) if p["kind"] == "scheduled")
    r = client.post("/slot", data={"plan_id": planned, "slot": f"{d4.isoformat()} 08:00", "medication_id": med["id"],
                                   "back": "/", "status": "taken", "when": "at", "at_time": "08:10", "confirm": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "Check before giving this one" not in html.unescape(client.get(f"/?d={d4.isoformat()}").text)


def store_plans(cfg, mid):
    with session(cfg.db_path) as conn:
        return store.plans(conn, mid)


# ============================================================================ S7


@pytest.fixture()
def cycle_db(cfg, monkeypatch):
    monkeypatch.setattr(schedule, "_TZ", cfg.tz)  # create_app sets this
    with session(cfg.db_path, write=True) as conn:
        store.create_cycle(conn, {"number": "1", "day1": DAY1.isoformat(), "length_days": "14"})
        put_settings(conn, {"onboarded": "1"})
    return cfg


def olanzapine(cfg, prn_days: tuple[int, int]) -> int:
    """Olanzapine at bedtime (9 PM) on days 2-4, and allowed as needed on `prn_days`."""
    with session(cfg.db_path, write=True) as conn:
        mid = store.add_from_catalog(conn, "olanzapine")
        for p in store.plans(conn, mid):
            store.delete_plan(conn, p["id"])
        store.create_plan(conn, mid, plan_values("scheduled", 2, 4, ["21:00"], "At bedtime"))
        store.create_plan(conn, mid, plan_values("as_needed", *prn_days))
    return mid


def bedtime_log(cfg, mid, d: date, now: datetime):
    with session(cfg.db_path) as conn:
        return next(s for s in schedule.day_plan(conn, d, now).scheduled if s.plan["medication_id"] == mid).log


def test_bug_small_hours_dose_on_an_as_needed_day_fills_last_nights_bedtime_dose(cycle_db):
    """S7. Day 4's 9 PM dose is given late, at 12:30 AM on Day 5, a day the medicine is allowed as needed. The dose
    was never paired, so "From last night: not logged" stayed on Today until noon and invited a second dose."""
    mid = olanzapine(cycle_db, (5, 6))
    with session(cycle_db.db_path, write=True) as conn:
        store.log_dose(conn, mid, at(D5, 0, 30), None, None)  # a "Log a dose" row from before slots were stored
    log = bedtime_log(cycle_db, mid, D4, at(D5, 1, 0))
    assert log is not None and log["taken_at"].endswith("00:30"), "Day 4's bedtime dose still shows as not logged"


def test_bug_route_log_a_dose_after_midnight_on_an_as_needed_day_closes_last_nights_dose(client, cycle_db, monkeypatch):
    """S7 through "Log a dose": at 1 AM on Day 5 someone logs the 12:30 AM dose. Today must not keep last night's
    bedtime dose as not logged."""
    mid = olanzapine(cycle_db, (5, 6))
    freeze(monkeypatch, at(D5, 1, 0))
    r = client.post("/dose/new", data={"medication_id": mid, "ago": "pick", "pick_time": "00:30", "dose": "1 tablet",
                                       "back": "/"}, follow_redirects=False)
    assert r.status_code == 303, html.unescape(r.text)[:1500]
    page = html.unescape(client.get("/").text)
    assert 'id="c-lastnight"' not in page, "From last night still lists the dose given at 12:30 AM"
    with session(cycle_db.db_path) as conn:
        row = conn.execute("SELECT slot, plan_id FROM dose_logs WHERE medication_id = ?", (mid,)).fetchone()
    assert row["slot"] == f"{D4.isoformat()} 21:00"


def test_fine_small_hours_dose_is_left_alone_when_both_nights_allow_it_as_needed(cycle_db):
    """Allowed as needed on Day 4 and Day 5: the 12:30 AM dose may be the as-needed one, so it isn't guessed."""
    mid = olanzapine(cycle_db, (4, 6))
    with session(cycle_db.db_path, write=True) as conn:
        store.log_dose(conn, mid, at(D5, 0, 30), None, None)
    assert bedtime_log(cycle_db, mid, D4, at(D5, 1, 0)) is None


def test_fine_small_hours_dose_still_fills_last_night_when_only_last_night_allowed_it(cycle_db):
    """Allowed as needed on days 3-4 only: 12:30 AM on Day 5 can't be an as-needed dose, so it is last night's."""
    mid = olanzapine(cycle_db, (3, 4))
    with session(cycle_db.db_path, write=True) as conn:
        store.log_dose(conn, mid, at(D5, 0, 30), None, None)
    assert bedtime_log(cycle_db, mid, D4, at(D5, 1, 0)) is not None


# ============================================================================ early-dose warning names


def test_bug_early_dose_warning_names_whoever_last_changed_the_entry(client, cfg):
    """John logs a Took one, then Jane corrects its time on her phone. When John taps Took one again, the warning
    said "You logged one", although Jane's version is what's saved. dose_warning already names updated_by."""
    setup_walkthrough(client, today(cfg) - timedelta(days=3))  # Day 4: prochlorperazine OK as needed, 6 hr apart
    jane = TestClient(client.app)
    with session(cfg.db_path) as conn:
        med = med_row(conn, "Prochlorperazine")
        people = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM profiles")}
    jane.post("/who", data={"profile_id": people["Jane"], "next": "/"})
    took = {"medication_id": med["id"], "ago": "0", "back": "/"}
    assert client.post("/dose/quick", data=took, follow_redirects=False).status_code == 303
    with session(cfg.db_path) as conn:
        row = conn.execute("SELECT * FROM dose_logs WHERE medication_id = ?", (med["id"],)).fetchone()
    earlier = schedule.parse_local(row["taken_at"]) - timedelta(minutes=10)
    r = jane.post(f"/dose/{row['id']}", data={"seen": row["updated_at"], "status": "taken", "back": "/",
                                              "taken_at": earlier.strftime("%Y-%m-%dT%H:%M"), "dose": row["dose"] or ""},
                  follow_redirects=False)
    assert r.status_code == 303, html.unescape(r.text)[:1500]
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT updated_by FROM dose_logs WHERE id = ?", (row["id"],)).fetchone()[0] == people["Jane"]
    johns = warning_of(client.post("/dose/quick", data=took).text)
    assert "Jane logged one" in johns and "You logged one" not in johns, johns
    janes = warning_of(jane.post("/dose/quick", data=took).text)
    assert "You logged one" in janes and "John logged one" not in janes, janes


def warning_of(page: str) -> str:
    """The sentence a confirm page starts its warning with."""
    text = html.unescape(page)
    found = re.search(r"[A-Z][^<>.]* one at [^<>]*?\.", text)
    assert found, "no warning on the page"
    return found.group(0)


# ============================================================================ S3 reminder check crash


def test_bug_a_crash_in_the_reminder_check_shows_in_settings_and_clears(client, cfg, monkeypatch):
    """S3. If working out the reminders raised, the loop only logged it: Settings looked healthy while nothing went
    out. It must show in plain words, and go away after the next check that works."""
    day = today(cfg)
    setup_walkthrough(client, day - timedelta(days=3))
    with session(cfg.db_path, write=True) as conn:
        put_settings(conn, {"ntfy_enabled": "1", "ntfy_topic": "chemotracker-test", "ntfy_chemo": "0"})
    nudger = client.app.state.reminders
    nudger.now = lambda: at(day, 11, 0)  # nothing is due at 11 AM
    working = reminders.due_reminders

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(reminders, "due_reminders", broken)
    passes = iter([False, True])  # one pass of the real background loop, then stop
    monkeypatch.setattr(nudger._stop, "wait", lambda timeout=None: next(passes))
    nudger._loop()
    assert nudger.last_error, "the reminder check failed but nothing records it"
    shown = reminders.problem_text(nudger.last_error)
    assert "check" in shown and "didn't go through" not in shown, shown
    page = html.unescape(client.get("/settings").text)
    assert shown in page and "database disk image is malformed" in page
    monkeypatch.setattr(reminders, "due_reminders", working)
    sent: list = []
    nudger.tick(lambda prefs, r: sent.append(r))
    assert sent == [] and nudger.last_error is None, nudger.last_error
    assert shown not in html.unescape(client.get("/settings").text)


def test_fine_a_good_check_with_nothing_to_send_keeps_a_send_problem(client, cfg):
    """A reminder that couldn't be sent stays in Settings until one goes through (the server may still be down)."""
    day = today(cfg)
    setup_walkthrough(client, day - timedelta(days=3))
    with session(cfg.db_path, write=True) as conn:
        put_settings(conn, {"ntfy_enabled": "1", "ntfy_topic": "chemotracker-test", "ntfy_chemo": "0"})
    nudger = client.app.state.reminders
    nudger.now = lambda: at(day, 11, 0)
    nudger.last_error = "couldn't reach https://ntfy.sh (timed out)"
    assert nudger.tick(lambda prefs, r: None) == 0
    assert nudger.last_error == "couldn't reach https://ntfy.sh (timed out)"
