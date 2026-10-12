"""Logging leftovers from the safety audit: each test reproduces one finding from the audit notes.

A clinic dose given after midnight (saved a day early, so the 24 hours before a white-cell booster looked
over), stale-page stamps after a put back or two saves in one second, and a symptom put back twice.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import chemotracker.db
import chemotracker.store
from chemotracker import schedule, store, web
from chemotracker.db import session

from .conftest import setup_walkthrough, today

LA = ZoneInfo("America/Los_Angeles")


def _plan(conn, med_name, kind="scheduled"):
    return conn.execute("""SELECT p.* FROM plans p JOIN medications m ON m.id = p.medication_id
                            WHERE m.name = ? AND p.kind = ?""", (med_name, kind)).fetchone()


def _med(conn, name):
    return conn.execute("SELECT * FROM medications WHERE name = ?", (name,)).fetchone()


def _doses(conn, med_name):
    return conn.execute("""SELECT dl.* FROM dose_logs dl JOIN medications m ON m.id = dl.medication_id
                            WHERE m.name = ? AND dl.status = 'taken' ORDER BY dl.id""", (med_name,)).fetchall()


def _clinic_slot(conn, d, med_name):
    return next(s for s in schedule.slots_on(conn, d) if s.plan["med_name"] == med_name and s.kind == "clinic")


def _flash_after(client, r) -> str:
    """The one-time message a redirect queued, read from the next page."""
    assert r.status_code == 303, r.text
    return html.unescape(client.get(r.headers["location"]).text)


def _freeze_web(monkeypatch, instant: datetime) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.astimezone(LA).replace(tzinfo=None)

    monkeypatch.setattr(web, "datetime", Frozen)


# ---------------------------------------------------------------- 1. clinic dose after midnight


def test_a_clinic_dose_given_after_midnight_is_saved_on_the_night_after_the_clinic_day(client, cfg):
    # An infusion that ran late, given at 12:30 AM. The family logs it on the clinic day's row with Other time.
    # It happened in the early hours after the clinic day, so that's where it goes. Filgrastim's label wants
    # at least 24 hours after chemo, and a time saved a day early made that check pass when it should warn.
    setup_walkthrough(client, today(cfg) - timedelta(days=3))
    d1, d2 = today(cfg) - timedelta(days=3), today(cfg) - timedelta(days=2)
    with session(cfg.db_path) as conn:
        dox, fil = _plan(conn, "Doxorubicin", "clinic"), _plan(conn, "Filgrastim")
    r = client.post("/slot", data={"plan_id": dox["id"], "slot": d1.isoformat(), "status": "taken", "when": "at",
                                   "at_time": "00:30", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        [given] = _doses(conn, "Doxorubicin")
        assert given["taken_at"] == f"{d2} 00:30"
        assert _clinic_slot(conn, d1, "Doxorubicin").log["id"] == given["id"]  # still the clinic day's dose
    # Filgrastim at 9:00 AM that morning is 8.5 hours after chemo: ask first.
    r = client.post("/slot", data={"plan_id": fil["id"], "slot": f"{d2} 15:00", "status": "taken",
                                   "taken_at": f"{d2}T09:00", "back": "/"}, follow_redirects=False)
    text = html.unescape(r.text)
    assert r.status_code == 200 and "at least 24 hours after chemo" in text, text[:400]
    assert "Doxorubicin was logged at 12:30 AM, 8 hr 30 min before this dose" in text
    with session(cfg.db_path) as conn:
        assert _doses(conn, "Filgrastim") == []


def test_a_small_hours_clinic_time_that_hasnt_happened_yet_is_refused(client, cfg):
    # On the clinic day itself, 12:30 AM means tonight after midnight, which hasn't happened yet.
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        dox = _plan(conn, "Doxorubicin", "clinic")
    r = client.post("/slot", data={"plan_id": dox["id"], "slot": today(cfg).isoformat(), "status": "taken",
                                   "when": "at", "at_time": "00:30", "back": "/"}, follow_redirects=False)
    assert "That time hasn't happened yet. Log it after it's given." in _flash_after(client, r)
    with session(cfg.db_path) as conn:
        assert _doses(conn, "Doxorubicin") == []


def test_moving_a_clinic_dose_past_midnight_on_its_page_keeps_it_on_the_clinic_day(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))
    d1, d2 = today(cfg) - timedelta(days=3), today(cfg) - timedelta(days=2)
    with session(cfg.db_path) as conn:
        dox = _plan(conn, "Doxorubicin", "clinic")
    client.post("/slot", data={"plan_id": dox["id"], "slot": d1.isoformat(), "status": "taken", "when": "at",
                               "at_time": "23:30", "back": "/"}, follow_redirects=False)
    with session(cfg.db_path) as conn:
        [given] = _doses(conn, "Doxorubicin")
    # It really finished at 12:30 AM: corrected on the dose page, with the date that goes with it.
    r = client.post(f"/dose/{given['id']}", data={"seen": given["updated_at"], "status": "taken",
                                                 "taken_at": f"{d2}T00:30", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with session(cfg.db_path) as conn:
        assert store.dose_log(conn, given["id"])["taken_at"] == f"{d2} 00:30"
        slot = _clinic_slot(conn, d1, "Doxorubicin")
        assert slot.log is not None and slot.log["id"] == given["id"]  # not "Not logged" plus an extra dose


def test_log_a_dose_and_old_pages_put_a_clinic_dose_after_midnight_on_the_right_night(client, cfg, monkeypatch):
    d1 = date(2026, 2, 2)
    setup_walkthrough(client, d1)
    with session(cfg.db_path) as conn:
        dox, cyc, fil = _med(conn, "Doxorubicin"), _plan(conn, "Cyclophosphamide", "clinic"), _med(conn, "Filgrastim")
    _freeze_web(monkeypatch, datetime(2026, 2, 3, 1, 0, tzinfo=LA))  # 1:00 AM after the clinic day
    # Log a dose with Other time 12:30 AM: tonight's early hours, the day after the clinic day.
    r = client.post("/dose/new", data={"medication_id": dox["id"], "ago": "pick", "pick_time": "00:30",
                                       "clinic_ok": "1", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    # A Given button from a page that never asked when (from before that question): it means now.
    r = client.post("/slot", data={"plan_id": cyc["id"], "slot": d1.isoformat(), "status": "taken", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    with session(cfg.db_path) as conn:
        assert [x["taken_at"] for x in _doses(conn, "Doxorubicin")] == ["2026-02-03 00:30"]
        assert [x["taken_at"] for x in _doses(conn, "Cyclophosphamide")] == ["2026-02-03 01:00"]
        warning = schedule.booster_warning(conn, fil, datetime(2026, 2, 3, 9, 0))
        assert warning and "at least 24 hours after chemo" in warning


# ---------------------------------------------------------------- 2. stale-page stamps


def _seen(page_text: str) -> str:
    return html.unescape(re.search(r'name="seen" value="([^"]+)"', page_text).group(1))


def test_an_edit_page_opened_before_a_delete_is_stale_after_put_back(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        med = _med(conn, "Prochlorperazine")
        dose_id = store.log_dose(conn, med["id"], datetime.combine(y, datetime.min.time()).replace(hour=9), "1 tablet", None)
        sym_id = store.add_symptom(conn, {"symptom": "nausea", "severity": "2", "logged_at": f"{y}T09:30"},
                                   datetime.combine(y, datetime.min.time()))
    for kind, entry_id, edit in (("dose", dose_id, {"status": "taken", "taken_at": f"{y}T09:05", "dose": "2 tablets"}),
                                 ("symptom", sym_id, {"symptom": "nausea", "severity": "3", "logged_at": f"{y}T09:30"})):
        base = f"/dose/{entry_id}" if kind == "dose" else f"/symptoms/{entry_id}"
        old_seen = _seen(client.get(base).text)  # John opens the edit page
        assert client.post(f"{base}/delete", data={"back": "/"}, follow_redirects=False).status_code == 303
        with session(cfg.db_path) as conn:
            ev = conn.execute("SELECT id FROM history WHERE entity = ? AND ref = ? AND action = 'deleted' "
                              "ORDER BY id DESC LIMIT 1", (kind, str(entry_id))).fetchone()["id"]
        r = client.post(f"/history/{ev}/put-back", data={"back": "/history"}, follow_redirects=False)
        assert r.headers["location"] == base, r.headers["location"]
        # John saves the page he opened before the delete: it must say the entry changed, not overwrite it.
        r = client.post(base, data={"seen": old_seen, "back": "/", **edit}, follow_redirects=False)
        assert r.status_code == 409, (kind, r.status_code)
        with session(cfg.db_path) as conn:
            history_rows = conn.execute("SELECT action FROM history WHERE entity = ? AND ref = ? ORDER BY id",
                                        (kind, str(entry_id))).fetchall()
        assert [h["action"] for h in history_rows] == ["created", "deleted", "created"]  # no stamp-only line


def test_two_saves_in_the_same_second_still_change_the_seen_stamp(client, cfg, monkeypatch):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))
    y = today(cfg) - timedelta(days=1)
    instant = datetime(2026, 1, 5, 18, 0, 0, tzinfo=UTC)

    class Frozen(datetime):  # every stamp the app writes lands on the same instant
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(chemotracker.db, "datetime", Frozen)
    monkeypatch.setattr(chemotracker.store, "datetime", Frozen)
    with session(cfg.db_path, write=True) as conn:
        med = _med(conn, "Prochlorperazine")
        dose_id = store.log_dose(conn, med["id"], datetime.combine(y, datetime.min.time()).replace(hour=9), "1 tablet", None)
    old_seen = _seen(client.get(f"/dose/{dose_id}").text)  # John's page
    jane = client.post(f"/dose/{dose_id}", data={"seen": old_seen, "status": "taken", "taken_at": f"{y}T09:00",
                                                 "dose": "2 tablets", "back": "/"}, follow_redirects=False)
    assert jane.status_code == 303
    john = client.post(f"/dose/{dose_id}", data={"seen": old_seen, "status": "taken", "taken_at": f"{y}T09:00",
                                                 "dose": "1 tablet", "notes": "John", "back": "/"}, follow_redirects=False)
    assert john.status_code == 409
    with session(cfg.db_path) as conn:
        assert store.dose_log(conn, dose_id)["dose"] == "2 tablets"


def test_put_back_refuses_a_symptom_that_is_already_there_at_that_time(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        first = store.add_symptom(conn, {"symptom": "nausea", "severity": "2", "logged_at": f"{y}T09:30"},
                                  datetime.combine(y, datetime.min.time()))
    client.post(f"/symptoms/{first}/delete", data={"back": "/"}, follow_redirects=False)
    with session(cfg.db_path, write=True) as conn:  # logged again at the same time after the delete
        store.add_symptom(conn, {"symptom": "nausea", "severity": "2", "logged_at": f"{y}T09:30"},
                          datetime.combine(y, datetime.min.time()))
        ev = conn.execute("SELECT id FROM history WHERE entity = 'symptom' AND action = 'deleted'").fetchone()["id"]
    r = client.post(f"/history/{ev}/put-back", data={"back": "/history"}, follow_redirects=False)
    assert (f"There's already a Nausea entry for {y:%b} {y.day}, 9:30 AM, so this one wasn't put back."
            in _flash_after(client, r))
    with session(cfg.db_path) as conn:
        assert len(store.symptoms_on(conn, y)) == 1
