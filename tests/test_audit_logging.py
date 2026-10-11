"""Audit: every way a dose is created, edited, undone or deleted, and every way one dose can be recorded twice.

Each test describes what a family should be able to rely on. A failing test is a finding (see
audit-findings/logging.md). Tests named test_ok_* are checks that passed: behaviour that was fine.
Demo people only: John (patient, this device) and Jane (a second phone).
"""

from __future__ import annotations

import html
import re
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from chemotracker import schedule, web
from chemotracker.db import connect, session

from .conftest import local_now, setup_walkthrough, today

# ---------------------------------------------------------------- helpers


def _plan(conn, med_name, kind="scheduled"):
    return conn.execute("""SELECT p.* FROM plans p JOIN medications m ON m.id = p.medication_id
                            WHERE m.name = ? AND p.kind = ?""", (med_name, kind)).fetchone()


def _med(conn, name):
    return conn.execute("SELECT id FROM medications WHERE name = ?", (name,)).fetchone()["id"]


def _slots(conn, d, med_name):
    cycle = schedule.cycle_for_date(conn, d)
    return [s for s in schedule.slots(conn, cycle, schedule.day_number(cycle, d)) if s.plan["med_name"] == med_name]


def _taken(conn, med_name, d=None):
    sql = """SELECT dl.* FROM dose_logs dl JOIN medications m ON m.id = dl.medication_id
              WHERE m.name = ? AND dl.status = 'taken'"""
    args = [med_name]
    if d is not None:
        sql += " AND substr(dl.taken_at, 1, 10) = ?"
        args.append(d.isoformat())
    return conn.execute(sql + " ORDER BY dl.id", args).fetchall()


def _device(client, cfg, name="Jane") -> TestClient:
    """A second phone: its own cookie, signed in as `name`."""
    other = TestClient(client.app)
    with session(cfg.db_path) as conn:
        pid = conn.execute("SELECT id FROM profiles WHERE name = ?", (name,)).fetchone()["id"]
    r = other.post("/who", data={"profile_id": str(pid), "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    return other


def _hidden(page: str, action: str) -> dict:
    """The hidden fields a confirm page will send back when its button is tapped."""
    form = page.split(f'action="{action}"')[1].split("</form>")[0]
    return {html.unescape(k): html.unescape(v)
            for k, v in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)}


def _as_if_reused(cfg, new_id: int, old_id: int) -> None:
    """Since the v5 upgrade SQLite never hands a freed dose id to a new dose. A record from before it can still
    hold such a pair, so give the new dose the freed id the way SQLite did, history included."""
    conn = connect(cfg.db_path)
    try:
        conn.execute("UPDATE dose_logs SET id = ? WHERE id = ?", (old_id, new_id))
        conn.execute("UPDATE history SET ref = ? WHERE entity = 'dose' AND ref = ?", (str(old_id), str(new_id)))
    finally:
        conn.close()


def _tap(device, plan, key, **extra):
    data = {"plan_id": plan["id"], "slot": key, "status": "taken", "back": "/", **extra}
    return device.post("/slot", data=data, follow_redirects=False)


@pytest.fixture()
def clock(monkeypatch):
    """Move the app's clock forward (a page left open for hours) without waiting."""
    class Clock(datetime):
        shift = timedelta(0)

        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + cls.shift

    monkeypatch.setattr(web, "datetime", Clock)
    return Clock


# ---------------------------------------------------------------- Undo and delete


def test_undo_after_replace_it_with_mine_keeps_the_original_dose(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # yesterday was Day 2: Filgrastim at 3:00 PM
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    assert _tap(client, plan, f"{y} 15:00", taken_at=f"{y}T15:05").status_code == 303  # John gave the shot
    jane = _device(client, cfg)
    r = _tap(jane, plan, f"{y} 15:00", taken_at=f"{y}T15:20")
    assert r.status_code == 409 and "Already recorded" in r.text
    assert jane.post("/slot", data=_hidden(r.text, "/slot"), follow_redirects=False).status_code == 303  # Replace it with mine
    with session(cfg.db_path) as conn:
        log_id = _taken(conn, "Filgrastim", y)[0]["id"]
    # Jane sees John's entry was right after all and taps Undo on her message, to undo her replace.
    jane.post(f"/dose/{log_id}/undo", data={"back": "/"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        left = _taken(conn, "Filgrastim", y)
        row = _slots(conn, y, "Filgrastim")[0]
    assert left, ("Undo after 'Replace it with mine' deleted the shot John logged: no Filgrastim record is left "
                  f"and the row shows {row.status_text(local_now(cfg))!r}, inviting a second shot")
    assert left[0]["taken_at"] == f"{y} 15:05"


def test_an_undo_left_on_screen_does_not_delete_a_dose_someone_else_has_since_confirmed(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    _tap(client, plan, f"{y} 15:00", taken_at=f"{y}T15:05")  # John taps Taken before giving it
    with session(cfg.db_path) as conn:
        shot_id = _taken(conn, "Filgrastim", y)[0]["id"]
    assert f'action="/dose/{shot_id}/undo"' in client.get(f"/?d={y}").text  # John's phone: the row's Undo
    jane = _device(client, cfg)
    r = _tap(jane, plan, f"{y} 15:00", taken_at=f"{y}T15:20")  # Jane gave it; her Taken says Already recorded
    jane.post("/slot", data=_hidden(r.text, "/slot"), follow_redirects=False)  # Replace it with mine: her time
    # John, still looking at his old page, taps the row's Undo because he never gave the shot himself.
    client.post(f"/dose/{shot_id}/undo", data={"back": "/"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        left = _taken(conn, "Filgrastim", y)
    assert left, "John's Undo from an old page deleted the shot Jane gave and confirmed at 3:20 PM; nothing warned him"


def test_an_old_undo_button_never_removes_a_different_dose(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        fil, dex = _plan(conn, "Filgrastim"), _plan(conn, "Dexamethasone")
    _tap(client, fil, f"{y} 15:00", taken_at=f"{y}T15:05")
    with session(cfg.db_path) as conn:
        shot_id = _taken(conn, "Filgrastim", y)[0]["id"]
    jane = _device(client, cfg)
    assert f'action="/dose/{shot_id}/undo"' in jane.get(f"/?d={y}").text  # Jane's phone shows the shot and its Undo
    # John undoes his own entry, then logs the morning Dexamethasone. SQLite hands it the freed id.
    client.post(f"/dose/{shot_id}/undo", data={"back": "/"})
    _tap(client, dex, f"{y} 08:00", taken_at=f"{y}T08:05")
    with session(cfg.db_path) as conn:
        dex_id = _taken(conn, "Dexamethasone", y)[0]["id"]
    assert dex_id != shot_id  # v5: SQLite no longer hands it the freed id...
    _as_if_reused(cfg, dex_id, shot_id)  # ...but a record from before the upgrade can have one
    with session(cfg.db_path) as conn:
        assert _taken(conn, "Dexamethasone", y)[0]["id"] == shot_id  # same id, different dose
    # Jane taps Undo on the Filgrastim row her phone still shows.
    jane.post(f"/dose/{shot_id}/undo", data={"back": "/"})
    with session(cfg.db_path) as conn:
        assert _taken(conn, "Dexamethasone", y), \
            "Jane's Undo on the old Filgrastim row deleted John's Dexamethasone dose (its id was reused)"


# ---------------------------------------------------------------- confirm pages submitted later or twice


def test_log_it_anyway_from_an_old_warning_page_never_overwrites_a_dose_logged_since(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        plan = _plan(conn, "Dexamethasone")
        conn.execute("UPDATE plans SET times = '08:00,20:00' WHERE id = ?", (plan["id"],))
    _tap(client, plan, f"{y} 08:00", taken_at=f"{y}T08:05")  # John, morning dose
    jane = _device(client, cfg)
    r = _tap(jane, plan, f"{y} 20:00", taken_at=f"{y}T08:30")  # Jane, by mistake on the evening row
    assert r.status_code == 200 and "Check before you log this" in r.text
    fields = _hidden(r.text, "/slot")
    # Jane leaves the warning on screen. At 8 PM John gives the evening dose and logs it.
    assert _tap(client, plan, f"{y} 20:00", taken_at=f"{y}T20:02").status_code == 303
    # Later Jane taps "Log it anyway" on the page she left open.
    r = jane.post("/slot", data=fields, follow_redirects=False)
    with session(cfg.db_path) as conn:
        evening = next(s for s in _slots(conn, y, "Dexamethasone") if s.time == "20:00")
    assert evening.log["taken_at"] == f"{y} 20:02", (
        f"John's 8:02 PM dose was silently rewritten to {evening.log['taken_at']} (HTTP {r.status_code}); "
        "Jane was never told it had been logged")


def test_a_double_tap_on_log_it_anyway_does_not_log_the_pill_twice(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # today is Day 3: Prochlorperazine as needed, 6 h apart
    with session(cfg.db_path) as conn:
        med = _med(conn, "Prochlorperazine")
    first = (local_now(cfg) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    client.post("/dose/quick", data={"medication_id": med, "taken_at": first, "back": "/"})
    r = client.post("/dose/quick", data={"medication_id": med, "ago": "0", "back": "/"}, follow_redirects=False)
    assert "Check before you log this" in r.text
    fields = _hidden(r.text, "/dose/quick")
    client.post("/dose/quick", data=fields, follow_redirects=False)  # Log it anyway
    client.post("/dose/quick", data=fields, follow_redirects=False)  # the same button again (slow phone)
    with session(cfg.db_path) as conn:
        n = len(_taken(conn, "Prochlorperazine"))
    assert n == 2, f"{n} Prochlorperazine records: a second tap on 'Log it anyway' logged the same pill again, no warning"


def test_replace_it_with_mine_keeps_the_time_the_page_showed(client, cfg, clock):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # today is Day 3: Filgrastim at 3:00 PM
    t = today(cfg)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    _tap(client, plan, f"{t} 15:00", when="now")  # John
    jane = _device(client, cfg)
    shown = local_now(cfg)
    r = _tap(jane, plan, f"{t} 15:00", when="now")  # Jane taps Taken, Just now: Already recorded
    assert r.status_code == 409 and schedule.fmt_time(shown) in r.text  # "You tapped ... at <now>"
    clock.shift = timedelta(hours=2)  # the page sits open for two hours, then she taps Replace it with mine
    jane.post("/slot", data=_hidden(r.text, "/slot"), follow_redirects=False)
    with session(cfg.db_path) as conn:
        stored = schedule.parse_local(_taken(conn, "Filgrastim")[0]["taken_at"])
    assert abs(stored - shown) <= timedelta(minutes=1), (
        f"page said {schedule.fmt_time(shown)}, record says {schedule.fmt_time(stored)}: "
        "the confirm page resent when=now instead of the time it showed")


# ---------------------------------------------------------------- editing a dose on its page


def test_moving_a_dose_to_another_day_does_not_leave_that_day_open_for_a_second_dose(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))  # two days ago was Day 2, yesterday Day 3
    d2, y = today(cfg) - timedelta(days=2), today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    # John logged the shot on Day 2's row, but it was really given on Day 3 at 8 AM. Jane fixes the date.
    _tap(client, plan, f"{d2} 15:00", taken_at=f"{d2}T15:05")
    with session(cfg.db_path) as conn:
        log = _taken(conn, "Filgrastim", d2)[0]
    jane = _device(client, cfg)
    r = jane.post(f"/dose/{log['id']}", data={"seen": log["updated_at"], "status": "taken", "taken_at": f"{y}T08:00",
                                              "dose": log["dose"] or "", "notes": "", "back": "/"},
                  follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        day2, day3 = _slots(conn, d2, "Filgrastim")[0], _slots(conn, y, "Filgrastim")[0]
    # Day 3's row still offers Taken ("Was due 3:00 PM"). That evening someone gives the shot it says is due.
    r = _tap(client, plan, f"{y} 15:00", taken_at=f"{y}T20:30")
    with session(cfg.db_path) as conn:
        shots = _taken(conn, "Filgrastim", y)
    assert len(shots) == 1, (
        f"{len(shots)} Filgrastim shots on Day 3 ({[s['taken_at'] for s in shots]}), logged with HTTP {r.status_code} "
        f"and no warning. Day 2's row says {day2.status_text(local_now(cfg))!r} (it was not given that day); "
        f"Day 3's row had log={day3.log is not None}")


def test_a_dose_cannot_be_moved_into_the_future_on_its_page(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # today is Day 3: Prochlorperazine as needed, 6 h apart
    with session(cfg.db_path) as conn:
        med = _med(conn, "Prochlorperazine")
    client.post("/dose/quick", data={"medication_id": med, "ago": "0", "back": "/"})
    with session(cfg.db_path) as conn:
        log = _taken(conn, "Prochlorperazine")[0]
    wrong = (local_now(cfg) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")  # date wheel moved one notch
    r = client.post(f"/dose/{log['id']}", data={"seen": log["updated_at"], "status": "taken", "taken_at": wrong,
                                                "dose": log["dose"] or "", "back": "/"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        stored = conn.execute("SELECT taken_at FROM dose_logs WHERE id = ?", (log["id"],)).fetchone()["taken_at"]
        cycle = schedule.cycle_for_date(conn, today(cfg))
        shelf = next(a for a in schedule.as_needed_for_day(conn, cycle, today(cfg), local_now(cfg)) if a.name == "Prochlorperazine")
    assert stored == log["taken_at"], (
        f"saved a dose time in the future ({stored}, HTTP {r.status_code}); the as-needed shelf now says "
        f"{shelf.status_text(local_now(cfg))!r} although the pill was just taken")


def test_clearing_the_time_on_a_dose_page_does_not_silently_move_the_dose_to_now(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    _tap(client, plan, f"{y} 15:00", taken_at=f"{y}T15:05")
    with session(cfg.db_path) as conn:
        log = _taken(conn, "Filgrastim", y)[0]
    # The time field was cleared (the phone's picker has a Clear button); only a note was meant to change.
    r = client.post(f"/dose/{log['id']}", data={"seen": log["updated_at"], "status": "taken", "taken_at": "",
                                                "dose": "", "notes": "Left thigh", "back": "/"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        stored = conn.execute("SELECT taken_at FROM dose_logs WHERE id = ?", (log["id"],)).fetchone()["taken_at"]
    assert stored == f"{y} 15:05" or r.status_code == 400, \
        f"yesterday's shot now says it was given {stored} (the moment of saving), and the page said 'Saved.'"


# ---------------------------------------------------------------- Change history: Put back


def test_put_back_does_not_bring_back_a_second_record_of_a_dose_that_was_logged_again(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        fil, dex, med = _plan(conn, "Filgrastim"), _plan(conn, "Dexamethasone"), _med(conn, "Filgrastim")
    _tap(client, fil, f"{y} 15:00", taken_at=f"{y}T15:05")
    _tap(client, dex, f"{y} 08:00", taken_at=f"{y}T08:05")  # so the shot isn't the newest record
    with session(cfg.db_path) as conn:
        shot = _taken(conn, "Filgrastim", y)[0]
    jane = _device(client, cfg)
    jane.post(f"/dose/{shot['id']}/delete", data={"back": "/"})  # removed by mistake...
    r = jane.post("/dose/new", data={"medication_id": med, "taken_at": f"{y}T15:05", "back": "/"},
                  follow_redirects=False)  # ...and logged again from "Log a dose"
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        assert len(_taken(conn, "Filgrastim", y)) == 1
        event = conn.execute("SELECT id FROM history WHERE entity = 'dose' AND action = 'deleted' AND ref = ?",
                             (str(shot["id"]),)).fetchone()["id"]
    # Later John finds the removed entry in Change history and taps Put back.
    client.post(f"/history/{event}/put-back", data={"back": "/history"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        shots = _taken(conn, "Filgrastim", y)
    assert len(shots) == 1, f"Put back made {len(shots)} Filgrastim records for one shot, without a warning"


# ---------------------------------------------------------------- times after midnight


def test_log_a_dose_after_midnight_counts_for_last_nights_bedtime_dose_not_tonights(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=4))  # Day 3 two days ago, Day 4 yesterday
    d3, d4 = today(cfg) - timedelta(days=2), today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        med = _med(conn, "Olanzapine")  # Days 2-4 at bedtime (9:00 PM)
    # Day 3's bedtime tablet was given late, at 12:30 AM, and logged from "Log a dose".
    r = client.post("/dose/new", data={"medication_id": med, "taken_at": f"{d4}T00:30", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        last_night, tonight = _slots(conn, d3, "Olanzapine")[0], _slots(conn, d4, "Olanzapine")[0]
    assert tonight.log is None, (
        "Day 4's 9 PM bedtime row shows 'Taken 12:30 AM', so nobody gives that night's tablet; "
        f"Day 3's row (the one actually given) shows log={last_night.log is not None}")


def test_a_bedtime_dose_given_the_next_morning_is_not_recorded_the_morning_before(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=4))
    d3, d4 = today(cfg) - timedelta(days=2), today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Olanzapine")
    # "From last night" stays on Today until noon. Day 3's tablet was given at 7:30 the next morning.
    r = _tap(client, plan, f"{d3} 21:00", when="at", at_time="07:30")
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        stored = _taken(conn, "Olanzapine")[0]["taken_at"]
    assert stored == f"{d4} 07:30", f"recorded {stored}: 13.5 hours before the 9 PM dose was even due"


# ---------------------------------------------------------------- Took all / Took both


def test_took_all_asks_before_logging_two_doses_of_one_medicine_at_the_same_minute(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        plan = _plan(conn, "Dexamethasone")
        conn.execute("UPDATE plans SET times = '12:00,16:00' WHERE id = ?", (plan["id"],))  # both Afternoon
    items = [f"{plan['id']}|{y} 12:00", f"{plan['id']}|{y} 16:00"]
    r = client.post("/slots/taken", data={"slots[]": items, "when": "at", "at_time": "16:05", "back": "/"})
    with session(cfg.db_path) as conn:
        doses = _taken(conn, "Dexamethasone", y)
    toast = html.unescape(r.text.split('class="toast-text">')[1].split("<")[0]) if "toast-text" in r.text else ""
    assert len(doses) <= 1, (f"'Took both' logged {len(doses)} Dexamethasone doses at "
                             f"{sorted({d['taken_at'] for d in doses})} with no warning ({toast!r})")


def test_took_all_does_not_say_someone_else_logged_what_you_logged(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))  # yesterday was Day 3
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        dex = _plan(conn, "Dexamethasone")
        conn.execute("""INSERT INTO plans (medication_id, kind, start_day, end_day, times, from_cycle, created_at)
                        SELECT id, 'scheduled', 1, NULL, '08:00', 1, datetime('now') FROM medications WHERE name = 'Olanzapine'""")
        items = [f"{s.plan['id']}|{s.key}" for s in _slots(conn, y, "Dexamethasone") + _slots(conn, y, "Olanzapine")
                 if s.time == "08:00"]
    _tap(client, dex, f"{y} 08:00", taken_at=f"{y}T08:05")  # John taps one row...
    r = client.post("/slots/taken", data={"slots[]": items, "when": "at", "at_time": "08:10", "back": "/"})  # ...then Took both
    text = html.unescape(r.text)
    assert "already logged by someone else" not in text, "John is told someone else logged the dose he logged himself"


# ---------------------------------------------------------------- skipped vs taken


def test_a_skipped_dose_given_later_from_log_a_dose_is_not_left_as_skipped(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan, med = _plan(conn, "Dexamethasone"), _med(conn, "Dexamethasone")
    client.post("/slot", data={"plan_id": plan["id"], "slot": f"{y} 08:00", "status": "skipped", "back": "/"})
    jane = _device(client, cfg)
    r = jane.post("/dose/new", data={"medication_id": med, "taken_at": f"{y}T11:00", "back": "/"}, follow_redirects=False)
    asked = r.status_code == 200 and "Check before you log this" in r.text
    with session(cfg.db_path) as conn:
        row = _slots(conn, y, "Dexamethasone")[0]
        extras = schedule.extra_doses(conn, schedule.cycle_for_date(conn, y), y)
    assert asked or row.log["status"] == "taken", (
        f"the 8 AM row still says {row.status_text(local_now(cfg))!r} and the 11 AM dose is listed as "
        f"{[e['note'] for e in extras]}: the only dose given that day is called 'More than planned'")


# ---------------------------------------------------------------- follow-up answers


def test_a_late_follow_up_answer_never_lands_on_a_different_dose(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # today is Day 3
    with session(cfg.db_path) as conn:
        med, dex = _med(conn, "Prochlorperazine"), _plan(conn, "Dexamethasone")
    two_h = (local_now(cfg) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    client.post("/dose/quick", data={"medication_id": med, "taken_at": two_h, "back": "/"})
    with session(cfg.db_path) as conn:
        pill = _taken(conn, "Prochlorperazine")[0]
    jane = _device(client, cfg)
    assert f'action="/dose/{pill["id"]}/follow"' in jane.get("/").text  # Jane's phone asks "Did it help?"
    # John removes that pill (logged by mistake) and logs the morning Dexamethasone, which reuses the id.
    client.post(f"/dose/{pill['id']}/undo", data={"back": "/"})
    _tap(client, dex, f"{today(cfg)} 08:00", when="now")
    with session(cfg.db_path) as conn:
        dex_id = _taken(conn, "Dexamethasone", None)[0]["id"]
    assert dex_id != pill["id"]  # v5: SQLite no longer hands it the freed id...
    _as_if_reused(cfg, dex_id, pill["id"])  # ...but a record from before the upgrade can have one
    with session(cfg.db_path) as conn:
        assert _taken(conn, "Dexamethasone", None)[0]["id"] == pill["id"]
    r = jane.post(f"/dose/{pill['id']}/follow", data={"helped": "no", "dose_feel": "too_strong", "back": "/"})
    with session(cfg.db_path) as conn:
        steroid = _taken(conn, "Dexamethasone", None)[0]
    assert steroid["helped"] is None and steroid["dose_feel"] is None, (
        f"Jane's answer about the nausea pill (Did it help: {steroid['helped']}, dose {steroid['dose_feel']}) "
        f"was saved on John's Dexamethasone dose; she was told {'Thanks, noted' if 'Thanks, noted' in r.text else r.status_code}")


# ---------------------------------------------------------------- pages opened before a schedule change


def _move(client, plan, times, end_day):
    r = client.post(f"/plans/{plan['id']}", data={"kind": "scheduled", "start_day": str(plan["start_day"]),
                                                  "end_day": str(end_day), "times": times,
                                                  "label": plan["label"] or "", "from_cycle": "1"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text


def test_taken_on_a_row_from_before_a_schedule_change_still_records_the_dose(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # yesterday was Day 2: Filgrastim at 3:00 PM
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    jane = _device(client, cfg)
    assert f'value="{y} 15:00"' in jane.get(f"/?d={y}").text  # Jane's phone shows the 3:00 PM row
    _move(client, plan, "12:30", 7)  # John moves the shot to 12:30 PM on the Medicines page
    r = _tap(jane, plan, f"{y} 15:00", when="at", at_time="15:10")  # Jane gives it and taps Taken on her page
    with session(cfg.db_path) as conn:
        shots = _taken(conn, "Filgrastim", y)
        row = _slots(conn, y, "Filgrastim")[0]
    said = "That page doesn't exist." if "That page doesn't exist" in html.unescape(r.text) else r.text[:80]
    assert shots, (f"the shot Jane gave was not recorded (HTTP {r.status_code}, page says {said!r}); "
                   f"the 12:30 PM row still says {row.status_text(local_now(cfg))!r}")


def test_took_all_on_a_page_from_before_a_schedule_change_does_not_silently_drop_a_dose(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))  # yesterday was Day 3
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        dex = _plan(conn, "Dexamethasone")
        conn.execute("""INSERT INTO plans (medication_id, kind, start_day, end_day, times, from_cycle, created_at)
                        SELECT id, 'scheduled', 1, NULL, '08:00', 1, datetime('now') FROM medications WHERE name = 'Olanzapine'""")
        items = [f"{s.plan['id']}|{s.key}" for s in _slots(conn, y, "Dexamethasone") + _slots(conn, y, "Olanzapine")
                 if s.time == "08:00"]
    jane = _device(client, cfg)
    _move(client, dex, "08:30", 4)  # John moves Dexamethasone to 8:30 while Jane's page is open
    r = jane.post("/slots/taken", data={"slots[]": items, "when": "at", "at_time": "08:10", "back": "/"})
    toast = html.unescape(r.text.split('class="toast-text">')[1].split("<")[0])
    with session(cfg.db_path) as conn:
        given = _taken(conn, "Dexamethasone", y)
    assert given or "Dexamethasone" in toast, (
        f"'Took both' said {toast!r}; the Dexamethasone given with it was dropped without a word and its "
        "8:30 row still shows as not logged")


# ---------------------------------------------------------------- more on reused ids and clock times


def test_a_removed_dose_can_still_be_put_back_after_its_id_is_reused(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        fil, dex = _plan(conn, "Filgrastim"), _plan(conn, "Dexamethasone")
    _tap(client, fil, f"{y} 15:00", taken_at=f"{y}T15:05")
    with session(cfg.db_path) as conn:
        shot_id = _taken(conn, "Filgrastim", y)[0]["id"]
    client.post(f"/dose/{shot_id}/delete", data={"back": "/"})  # removed by mistake
    _tap(client, dex, f"{y} 08:00", taken_at=f"{y}T08:05")
    with session(cfg.db_path) as conn:
        dex_id = _taken(conn, "Dexamethasone", y)[0]["id"]
    assert dex_id != shot_id  # v5: SQLite no longer hands it the freed id...
    _as_if_reused(cfg, dex_id, shot_id)  # ...but a record from before the upgrade can have one
    with session(cfg.db_path) as conn:
        assert _taken(conn, "Dexamethasone", y)[0]["id"] == shot_id
        event = conn.execute("SELECT id FROM history WHERE action = 'deleted' AND ref = ?", (str(shot_id),)).fetchone()["id"]
    offered = f"/history/{event}/put-back" in client.get("/history").text
    r = client.post(f"/history/{event}/put-back", data={"back": "/history"})
    said = "It's already back." if "already back" in html.unescape(r.text) else ""
    with session(cfg.db_path) as conn:
        shots = _taken(conn, "Filgrastim", y)
    assert shots, (f"the removed shot can't come back: Change history offers Put back={offered}, and Put back says "
                   f"{said!r} although no Filgrastim record exists (its id now belongs to a Dexamethasone dose)")


def test_other_time_later_than_now_in_the_daytime_is_not_logged_as_yesterday(client, cfg, clock):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # today is Day 3: Prochlorperazine as needed
    t = today(cfg)
    clock.shift = datetime.combine(t, time(14, 0)) - local_now(cfg)  # the app's clock reads 2:00 PM
    with session(cfg.db_path) as conn:
        med = _med(conn, "Prochlorperazine")
    # "Took one" with Other time: the minute wheel slips to 2:30 PM (the pill was taken at 1:30).
    r = client.post("/dose/quick", data={"medication_id": med, "ago": "pick", "pick_time": "14:30", "back": "/"},
                    follow_redirects=False)
    with session(cfg.db_path) as conn:
        stored = [d["taken_at"] for d in _taken(conn, "Prochlorperazine")]
        cycle = schedule.cycle_for_date(conn, t)
        shelf = next(a for a in schedule.as_needed_for_day(conn, cycle, t, datetime.combine(t, time(14, 0)))
                     if a.name == "Prochlorperazine")
    yesterday = (t - timedelta(days=1)).isoformat()
    assert not any(s.startswith(yesterday) for s in stored), (
        f"a 2:30 PM pick at 2:00 PM was saved as {stored} (HTTP {r.status_code}), a day early; the shelf now says "
        f"{shelf.status_text(datetime.combine(t, time(14, 0)))!r}")


def test_two_phones_signed_in_as_the_same_person_still_get_already_recorded(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # today is Day 3: Filgrastim at 3:00 PM
    t = today(cfg)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    tablet = _device(client, cfg, "John")  # the family tablet, also left on John
    assert _tap(client, plan, f"{t} 15:00", when="now").status_code == 303  # one carer gives the shot on the phone
    r = _tap(tablet, plan, f"{t} 15:00", when="now")  # a minute later another gives it and taps on the tablet
    with session(cfg.db_path) as conn:
        n = len(_taken(conn, "Filgrastim"))
    assert r.status_code == 409, (f"the second phone's Taken was treated as a double tap (HTTP {r.status_code}, "
                                  f"{n} record): no 'Already recorded', it was told the shot was marked taken")


# ---------------------------------------------------------------- checks that passed (kept as evidence)


def test_ok_two_phones_tapping_taken_on_one_row(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    jane = _device(client, cfg)
    assert _tap(client, plan, f"{y} 15:00", taken_at=f"{y}T15:05").status_code == 303
    r = _tap(jane, plan, f"{y} 15:00", taken_at=f"{y}T15:06")
    assert r.status_code == 409 and "John already marked Filgrastim taken at 3:05 PM" in html.unescape(r.text)
    assert _tap(client, plan, f"{y} 15:00", taken_at=f"{y}T15:05").status_code == 303  # John's own double tap
    with session(cfg.db_path) as conn:
        assert len(_taken(conn, "Filgrastim", y)) == 1


def test_ok_stale_edit_on_the_dose_page_is_refused(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan = _plan(conn, "Filgrastim")
    _tap(client, plan, f"{y} 15:00", taken_at=f"{y}T15:05")
    with session(cfg.db_path, write=True) as conn:
        log = _taken(conn, "Filgrastim", y)[0]
        conn.execute("UPDATE dose_logs SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (log["id"],))
        seen = "2000-01-01T00:00:00+00:00"  # Jane opens the page...
    jane = _device(client, cfg)
    _tap(jane, plan, f"{y} 15:00", taken_at=f"{y}T15:20", confirm="1")  # ...John's entry is replaced meanwhile
    r = client.post(f"/dose/{log['id']}", data={"seen": seen, "status": "taken", "taken_at": f"{y}T15:05",
                                                "dose": "", "back": "/"}, follow_redirects=False)
    assert r.status_code == 409 and "while you had it open" in html.unescape(r.text)


def test_ok_put_back_refuses_when_the_same_row_was_logged_again(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        plan, dex = _plan(conn, "Filgrastim"), _plan(conn, "Dexamethasone")
    _tap(client, plan, f"{y} 15:00", taken_at=f"{y}T15:05")
    _tap(client, dex, f"{y} 08:00", taken_at=f"{y}T08:05")
    with session(cfg.db_path) as conn:
        shot = _taken(conn, "Filgrastim", y)[0]
    client.post(f"/dose/{shot['id']}/delete", data={"back": "/"})
    _tap(client, plan, f"{y} 15:00", taken_at=f"{y}T15:10")
    with session(cfg.db_path) as conn:
        event = conn.execute("SELECT id FROM history WHERE action = 'deleted' AND ref = ?", (str(shot["id"]),)).fetchone()["id"]
    r = client.post(f"/history/{event}/put-back", data={"back": "/history"})
    assert "newer entry" in html.unescape(r.text)
    with session(cfg.db_path) as conn:
        assert len(_taken(conn, "Filgrastim", y)) == 1
