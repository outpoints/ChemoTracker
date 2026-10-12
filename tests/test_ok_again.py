"""'OK again' reminders for as-needed medicines, held overnight and folded into one morning reminder.

The family asked for a notification when an as-needed medicine (a rescue anti-nausea pill) is OK again, because
the patient sleeps at irregular hours and fixed times mean nothing for it. These stay AS NEEDED: the reminder
says the medicine is OK again if you need it, never that it is due, and nothing is ever marked missed.

Reminder tests call `reminders.due_reminders` (or one `ReminderScheduler.tick` with a fake sender) at fixed
local times. Nothing is sent anywhere.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta

import pytest

from chemotracker import reminders, schedule, store, web
from chemotracker.db import get_settings, put_settings, session

from .conftest import setup_walkthrough

# Made-up cycle. Day 1 = Mon May 4 2026 (no clock change near it), 14 days. Day 3 = May 6, Day 7 = May 10.
DAY1 = date(2026, 5, 4)
D3, D4, D7, D8 = (DAY1 + timedelta(days=n) for n in (2, 3, 6, 7))
URL = "http://192.0.2.10:8000"
NOTE = "Take it only if you need it, and follow the label."


def at(d: date, hh: int, mm: int = 0, fold: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, fold=fold)


@pytest.fixture()
def db(cfg, monkeypatch):
    monkeypatch.setattr(schedule, "_TZ", cfg.tz)  # create_app sets this; the live scheduler runs with it
    with session(cfg.db_path, write=True) as conn:
        store.create_cycle(conn, {"number": "1", "day1": DAY1.isoformat(), "length_days": "14"})
        put_settings(conn, {"ntfy_enabled": "1", "ntfy_topic": "chemotracker-test", "app_url": URL})
    return cfg


def add_prn(cfg, key: str, gap: float | None, max_per_day: int | None = None, start: int = 1, end: int = 7) -> int:
    """A catalog medicine whose only plan is 'as needed' on days start..end, with the label's gap and limit."""
    with session(cfg.db_path, write=True) as conn:
        mid = store.add_from_catalog(conn, key)
        for p in store.plans(conn, mid):
            store.delete_plan(conn, p["id"])
        store.create_plan(conn, mid, {"kind": "as_needed", "start_day": str(start), "end_day": str(end), "times": [],
                                      "label": "As needed", "dose": None, "from_cycle": None, "to_cycle": None})
        conn.execute("UPDATE medications SET min_hours_between = ?, max_per_day = ? WHERE id = ?", (gap, max_per_day, mid))
    return mid


def choose(cfg, *mids: int, morning: str = "09:00", quiet: str = "02:00", **more: str) -> None:
    with session(cfg.db_path, write=True) as conn:
        put_settings(conn, {"ntfy_ok_meds": ",".join(map(str, mids)), "ntfy_morning": morning,
                            "ntfy_quiet_from": quiet, **more})


def took(cfg, mid: int, when: datetime) -> int:
    with session(cfg.db_path, write=True) as conn:
        return store.log_dose(conn, mid, when, "1 tablet", None)


def due(cfg, now: datetime) -> list[reminders.Reminder]:
    with session(cfg.db_path) as conn:
        return reminders.due_reminders(conn, now, get_settings(conn))


def oks(rs) -> list[reminders.Reminder]:
    return [r for r in rs if r.key.startswith("ok|")]


def mornings(rs) -> list[reminders.Reminder]:
    return [r for r in rs if r.key.startswith("morning|")]


def tick(cfg, now: datetime) -> list[reminders.Reminder]:
    nudger = reminders.ReminderScheduler(cfg)
    nudger.now = lambda: now
    sent: list[reminders.Reminder] = []
    nudger.tick(lambda prefs, r: sent.append(r))
    return sent


def sweep(cfg, start: datetime, end: datetime, step: int = 1) -> list[reminders.Reminder]:
    out, t = [], start
    while t <= end:
        out += tick(cfg, t)
        t += timedelta(minutes=step)
    return out


# ============================================================================ the schedule helper


def test_limit_clears_when_the_oldest_counted_dose_turns_24_hours_old(cfg, monkeypatch):
    monkeypatch.setattr(schedule, "_TZ", cfg.tz)
    doses = [at(D3, 8), at(D3, 12), at(D3, 16)]
    # Gap 4 hours: 8 PM by the gap, but three already count in the 24 hours before it.
    assert schedule.limit_clears_at(doses, 3, at(D3, 20)) == at(D4, 8)
    assert schedule.limit_clears_at(doses, 4, at(D3, 20)) == at(D3, 20), "under the limit, the gap time stands"
    assert schedule.limit_clears_at(doses, 2, at(D3, 20)) == at(D4, 12), "two must age out to get under 2"
    assert schedule.limit_clears_at(doses, 3, at(D4, 9)) == at(D4, 9), "8 AM's dose stopped counting at 8 AM"


# ============================================================================ OK again


def test_ok_again_fires_when_the_gap_has_passed_not_a_minute_before_and_only_once(db):
    proc = add_prn(db, "prochlorperazine", 6)
    choose(db, proc)
    log_id = took(db, proc, at(D3, 10, 5))
    assert oks(due(db, at(D3, 16, 4))) == []
    [r] = oks(due(db, at(D3, 16, 5)))
    assert r.key == f"ok|{proc}|{log_id}|2026-05-06 16:05"
    assert r.title == "Prochlorperazine is OK again"
    assert r.message == f"It's been 6 hr since the last one at 10:05 AM. {NOTE}"
    assert r.priority == 3 and r.click == URL + "/#c-prn" and r.tags
    for word in ("due", "now", "missed"):
        assert word not in f"{r.title} {r.message}".lower()
    sent = [r.key for r in sweep(db, at(D3, 15, 30), at(D3, 17, 30)) if r.key.startswith("ok|")]
    assert sent == [r.key], sent


def test_no_ok_again_for_a_medicine_that_isnt_ticked_or_was_archived(db):
    proc = add_prn(db, "prochlorperazine", 6)
    ond = add_prn(db, "ondansetron", 8)
    choose(db, proc)
    took(db, ond, at(D3, 8))
    assert oks(due(db, at(D3, 16))) == []
    took(db, proc, at(D3, 10, 5))
    assert [r.title for r in oks(due(db, at(D3, 16, 5)))] == ["Prochlorperazine is OK again"]
    with session(db.db_path, write=True) as conn:
        store.archive_medication(conn, proc)
    assert oks(due(db, at(D3, 16, 5))) == [], "an archived medicine sends nothing, even if it's still ticked"


def test_no_ok_again_when_reminders_are_off(db):
    proc = add_prn(db, "prochlorperazine", 6)
    choose(db, proc)
    took(db, proc, at(D3, 10, 5))
    assert len(oks(due(db, at(D3, 16, 5)))) == 1
    choose(db, proc, ntfy_enabled="")
    assert tick(db, at(D3, 16, 5)) == []


def test_logging_a_new_dose_or_changing_the_last_ones_time_moves_it(db):
    proc = add_prn(db, "prochlorperazine", 6)
    choose(db, proc)
    first = took(db, proc, at(D3, 10, 5))
    with session(db.db_path, write=True) as conn:
        store.update_dose_log(conn, first, {"status": "taken", "taken_at": "2026-05-06T11:00", "dose": "1 tablet"},
                              at(D3, 11))
    assert oks(due(db, at(D3, 16, 5))) == []
    assert [r.key for r in oks(due(db, at(D3, 17)))] == [f"ok|{proc}|{first}|2026-05-06 17:00"]
    second = took(db, proc, at(D3, 16, 20))
    assert oks(due(db, at(D3, 17))) == []
    [r] = oks(due(db, at(D3, 22, 20)))
    assert r.key == f"ok|{proc}|{second}|2026-05-06 22:20" and "at 4:20 PM" in r.message


def test_deleting_the_last_dose_cancels_it(db):
    proc = add_prn(db, "prochlorperazine", 6)
    choose(db, proc)
    earlier = took(db, proc, at(D3, 4, 5))
    log_id = took(db, proc, at(D3, 10, 5))
    assert len(oks(due(db, at(D3, 16, 5)))) == 1
    with session(db.db_path, write=True) as conn:
        store.delete_dose_log(conn, log_id)
    assert oks(due(db, at(D3, 16, 5))) == []
    assert [r.key for r in oks(due(db, at(D3, 10, 5)))] == [f"ok|{proc}|{earlier}|2026-05-06 10:05"], \
        "the dose before it counts again" 


def test_the_rolling_24_hour_limit_moves_it_to_when_the_oldest_counted_dose_turns_24_hours_old(db):
    med = add_prn(db, "metoclopramide", 4, max_per_day=3)
    choose(db, med, quiet="off")
    for h in (8, 12):
        took(db, med, at(D3, h))
    last = took(db, med, at(D3, 16))
    assert oks(due(db, at(D3, 20))) == [], "the gap has passed, but 3 already count in 24 hours"
    assert oks(due(db, at(D4, 7, 59))) == []
    [r] = oks(due(db, at(D4, 8)))
    assert r.key == f"ok|{med}|{last}|2026-05-07 08:00"
    assert r.message == f"It's been 16 hr since the last one yesterday at 4:00 PM. {NOTE}"


def test_no_ok_again_when_the_next_ok_time_is_after_the_plan_ends(db):
    proc = add_prn(db, "prochlorperazine", 6)  # as needed Days 1-7
    choose(db, proc, quiet="off")
    took(db, proc, at(D7, 10))
    assert len(oks(due(db, at(D7, 16)))) == 1, "Day 7 is still covered"
    took(db, proc, at(D7, 22))
    assert oks(due(db, at(D8, 4))) == []
    choose(db, proc)  # and the morning after Day 7 has nothing to list
    assert mornings(due(db, at(D8, 9))) == []


def test_ok_again_overnight_is_held_and_folded_into_the_morning_reminder(db):
    proc = add_prn(db, "prochlorperazine", 6)
    choose(db, proc)  # hold from 2:00 AM until the 9:00 AM reminder
    log_id = took(db, proc, at(D3, 22, 10))
    night = sweep(db, at(D4, 1, 30), at(D4, 8, 59), step=5)
    assert night == [], [r.key for r in night]
    [m] = mornings(due(db, at(D4, 9)))
    assert m.key == "morning|2026-05-07" and m.title == "Your as-needed medicines"
    assert "Prochlorperazine: OK if you need it, since 4:10 AM" in m.message.splitlines()
    assert f"ok|{proc}|{log_id}|2026-05-07 04:10" in m.also
    later = [r.key for r in sweep(db, at(D4, 9), at(D4, 10), step=5)]
    assert later == ["morning|2026-05-07"], later


def test_a_quiet_window_crossing_midnight(db):
    proc = add_prn(db, "prochlorperazine", 6)
    ond = add_prn(db, "ondansetron", 8)
    choose(db, proc, ond, quiet="22:00")
    took(db, ond, at(D3, 13))  # OK at 9:00 PM, before the quiet window
    took(db, proc, at(D3, 17))  # OK at 11:00 PM, inside it
    assert [r.title for r in oks(due(db, at(D3, 21)))] == ["Ondansetron is OK again"]
    assert oks(due(db, at(D3, 23))) == []
    assert oks(due(db, at(D4, 0, 30))) == []
    [m] = mornings(due(db, at(D4, 9)))
    assert "Prochlorperazine: OK if you need it, since 11:00 PM" in m.message.splitlines()
    assert "Ondansetron: OK if you need it" in m.message.splitlines(), "OK since before the quiet window: no 'since'"


def test_ok_time_exactly_at_the_morning_reminder_is_one_notification(db):
    proc = add_prn(db, "prochlorperazine", 6)
    choose(db, proc)
    took(db, proc, at(D4, 3))  # awake at 3 AM, OK again at 9:00 AM, the morning reminder's time
    sent = sweep(db, at(D4, 8, 55), at(D4, 9, 50))
    assert [r.key for r in sent] == ["morning|2026-05-07"], [r.key for r in sent]
    assert "Prochlorperazine: OK if you need it" in sent[0].message.splitlines()


def test_nothing_is_ticked_by_default_and_a_pain_medicine_reads_the_same(db):
    """Opt-in only: after an upgrade no medicine sends anything, an opioid included. Once ticked it gets the same
    'only if you need it' wording as the nausea pills."""
    with session(db.db_path, write=True) as conn:
        pain = store.create_medication(conn, {"name": "Oxycodone", "dose": "1 tablet"})
        store.create_plan(conn, pain, {"kind": "as_needed", "start_day": "1", "end_day": None, "times": [],
                                       "label": "As needed for pain", "dose": None, "from_cycle": None, "to_cycle": None})
        conn.execute("UPDATE medications SET min_hours_between = 4 WHERE id = ?", (pain,))
    took(db, pain, at(D3, 10))
    assert oks(due(db, at(D3, 14))) == [] and mornings(due(db, at(D4, 9))) == []
    choose(db, pain)
    [r] = oks(due(db, at(D3, 14)))
    assert (r.title, r.message) == ("Oxycodone is OK again", f"It's been 4 hr since the last one at 10:00 AM. {NOTE}")


def test_scheduled_medicines_keep_their_times_through_the_overnight_hold(db):
    """Only as-needed reminders wait for the morning. A bedtime pill inside the hold is still reminded on time."""
    with session(db.db_path, write=True) as conn:
        olz = store.add_from_catalog(conn, "olanzapine")
        for p in store.plans(conn, olz):
            store.delete_plan(conn, p["id"])
        store.create_plan(conn, olz, {"kind": "scheduled", "start_day": "1", "end_day": "4", "times": ["23:30"],
                                      "label": "At bedtime", "dose": None, "from_cycle": None, "to_cycle": None})
    proc = add_prn(db, "prochlorperazine", 6)
    choose(db, proc, quiet="22:00")
    assert [r.key for r in due(db, at(D3, 23, 30))] == ["due|2026-05-06 23:30"]


# ============================================================================ morning reminder


def test_morning_reminder_lists_only_ticked_medicines_planned_today_with_their_state(db):
    proc = add_prn(db, "prochlorperazine", 6)
    ond = add_prn(db, "ondansetron", 8)
    meto = add_prn(db, "metoclopramide", 4, max_per_day=4)
    lora = add_prn(db, "lorazepam", 6, start=9, end=14)  # ticked but not planned today
    add_prn(db, "senna", None)  # not ticked (no gap saved)
    choose(db, proc, ond, meto, lora)
    took(db, proc, at(D4, 5, 20))
    for t in (at(D3, 13, 15), at(D3, 17, 15), at(D3, 21, 15), at(D4, 1, 15)):
        took(db, meto, t)
    assert mornings(due(db, at(D4, 8, 59))) == []
    [m] = mornings(due(db, at(D4, 9)))
    assert m.priority == 3 and m.click == URL + "/#c-prn"
    assert mornings(due(db, at(D4, 9, 45))) == [], "a late morning reminder is dropped, like the others"
    assert m.message.splitlines() == [
        "Metoclopramide: Reached 4 in 24 hours, OK at 1:15 PM",
        "Ondansetron: OK if you need it",
        "Prochlorperazine: OK at 11:20 AM",
        "Take one only if you need it, and follow the label.",
    ], m.message
    sent = [r.key for r in sweep(db, at(D4, 9), at(D4, 9, 50), step=5) if r.key.startswith("morning|")]
    assert sent == ["morning|2026-05-07"], "once a day"
    assert [r.key for r in mornings(due(db, at(D3 + timedelta(days=2), 9)))] == ["morning|2026-05-08"]


def test_no_morning_reminder_when_nothing_ticked_is_planned_today_or_nothing_is_ticked(db):
    lora = add_prn(db, "lorazepam", 6, start=9, end=14)
    proc = add_prn(db, "prochlorperazine", 6)
    choose(db, proc)
    assert len(mornings(due(db, at(D4, 9)))) == 1
    choose(db, lora)
    assert mornings(due(db, at(D4, 9))) == []
    choose(db)
    assert mornings(due(db, at(D4, 9))) == []


def test_morning_off_sends_no_morning_reminder_and_holds_nothing(db):
    proc = add_prn(db, "prochlorperazine", 6)
    choose(db, proc, morning="off")
    took(db, proc, at(D3, 22, 10))
    assert [r.title for r in oks(due(db, at(D4, 4, 10)))] == ["Prochlorperazine is OK again"]
    assert mornings(due(db, at(D4, 9))) == []


def test_names_off(db):
    proc = add_prn(db, "prochlorperazine", 6)
    choose(db, proc, ntfy_names="0")
    took(db, proc, at(D3, 10, 5))
    [r] = oks(due(db, at(D3, 16, 5)))
    assert (r.title, r.message) == ("A medicine is OK again", "Open ChemoTracker to see which one.")
    [m] = mornings(due(db, at(D4, 9)))
    assert "Prochlorperazine" not in m.title + m.message
    assert m.message == "Open ChemoTracker to see when each one is OK."


def test_fall_back_night_uses_real_time(cfg, monkeypatch):
    """Nov 1 2026, America/Los_Angeles: 2:00 AM PDT becomes 1:00 AM PST. 10:00 PM + 6 hours is 3:00 AM."""
    monkeypatch.setattr(schedule, "_TZ", cfg.tz)
    with session(cfg.db_path, write=True) as conn:
        store.create_cycle(conn, {"number": "1", "day1": "2026-10-29", "length_days": "14"})
        put_settings(conn, {"ntfy_enabled": "1", "ntfy_topic": "chemotracker-test"})
    proc = add_prn(cfg, "prochlorperazine", 6)
    choose(cfg, proc, quiet="off")
    took(cfg, proc, datetime(2026, 10, 31, 22, 0))
    nov1 = date(2026, 11, 1)
    assert oks(due(cfg, at(nov1, 1, 30, fold=1))) == []
    assert oks(due(cfg, at(nov1, 2, 59))) == []
    [r] = oks(due(cfg, at(nov1, 3)))
    assert r.message == f"It's been 6 hr since the last one yesterday at 10:00 PM. {NOTE}"
    assert oks(due(cfg, at(nov1, 3, 50))) == [], "45 minutes of real time later it's dropped"


# ============================================================================ Settings


def _ids(cfg) -> dict[str, int]:
    with session(cfg.db_path) as conn:
        return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM medications")}


def _extra_meds(cfg) -> tuple[int, int]:
    """Senna as needed with no gap saved, and Lorazepam as needed but archived."""
    with session(cfg.db_path, write=True) as conn:
        senna = store.add_from_catalog(conn, "senna")
        lora = store.add_from_catalog(conn, "lorazepam")
        conn.execute("UPDATE medications SET min_hours_between = 6 WHERE id = ?", (lora,))
        store.archive_medication(conn, lora)
    return senna, lora


BASE_FORM = {"ntfy_enabled": "1", "ntfy_topic": "chemotracker-test", "ntfy_server": "https://ntfy.sh",
             "ntfy_lead": "0", "ntfy_nag": "30", "ntfy_chemo": "1", "ntfy_names": "1", "app_url": ""}


def _reminders_form(page: str) -> str:
    return page.split('action="/settings/reminders"')[1].split("</form>")[0]


def test_settings_lists_as_needed_medicines_and_saves_only_allowed_values(client, cfg):
    setup_walkthrough(client, DAY1)
    senna, lora = _extra_meds(cfg)
    ids = _ids(cfg)
    proc, ond, dexa = ids["Prochlorperazine"], ids["Ondansetron"], ids["Dexamethasone"]
    form = html.unescape(_reminders_form(client.get("/settings").text))
    assert "As-needed medicines" in form
    assert "Tell me when Prochlorperazine is OK again" in form and "Tell me when Ondansetron is OK again" in form
    assert "Dexamethasone" not in form and "Lorazepam" not in form and "Olanzapine" not in form
    i = form.index(f'name="ntfy_ok_meds[]" value="{senna}"')
    senna_box = form[form.rindex("<input", 0, i):form.index("</label>", i)]
    assert "disabled" in senna_box and "Save the hours between doses on" in senna_box
    assert f'href="/medicines/{senna}?edit=1#gap"' in senna_box
    assert "As-needed reminders that come up overnight wait for the morning reminder. Scheduled doses still remind at their time." in form

    r = client.post("/settings/reminders", data={**BASE_FORM, "ntfy_ok_meds[]": [str(proc), str(dexa), str(lora), "abc",
                                                                                  str(senna)],
                                                 "ntfy_morning": "25:00", "ntfy_quiet_from": "13:00"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        prefs = get_settings(conn)
    assert (prefs["ntfy_ok_meds"], prefs["ntfy_morning"], prefs["ntfy_quiet_from"]) == (str(proc), "09:00", "02:00")

    client.post("/settings/reminders", data={**BASE_FORM, "ntfy_ok_meds[]": [str(ond), str(proc)],
                                             "ntfy_morning": "08:30", "ntfy_quiet_from": "23:30"})
    with session(cfg.db_path) as conn:
        prefs = get_settings(conn)
    assert prefs["ntfy_ok_meds"] == ",".join(sorted([str(proc), str(ond)], key=int))
    assert (prefs["ntfy_morning"], prefs["ntfy_quiet_from"]) == ("08:30", "23:30")

    client.post("/settings/reminders", data={**BASE_FORM, "ntfy_morning": "off", "ntfy_quiet_from": "off"})
    with session(cfg.db_path) as conn:
        prefs = get_settings(conn)
    assert (prefs["ntfy_ok_meds"], prefs["ntfy_morning"], prefs["ntfy_quiet_from"]) == ("", "off", "off")


def test_settings_form_round_trips_without_javascript(client, cfg):
    """A browser with scripts off submits exactly what the form holds: ticked boxes and selected options."""
    setup_walkthrough(client, DAY1)
    proc = _ids(cfg)["Prochlorperazine"]
    client.post("/settings/reminders", data={**BASE_FORM, "ntfy_ok_meds[]": [str(proc)], "ntfy_morning": "10:30",
                                             "ntfy_quiet_from": "01:00"})
    form = _reminders_form(client.get("/settings").text)
    assert "data-js-only" not in form
    data: dict[str, list[str]] = {}
    for tag in re.findall(r"<input[^>]*>", form):
        name = re.search(r'name="([^"]+)"', tag)
        if not name or "disabled" in tag:
            continue
        kind = (re.search(r'type="([^"]+)"', tag) or [None, "text"])[1]
        if kind == "checkbox" and "checked" not in tag:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        data.setdefault(name[1], []).append(html.unescape(value[1]) if value else "")
    for name, body in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form, re.DOTALL):
        chosen = re.search(r'<option value="([^"]*)" selected', body)
        data[name] = [chosen[1] if chosen else re.search(r'<option value="([^"]*)"', body)[1]]
    assert data["ntfy_ok_meds[]"] == [str(proc)]
    assert data["ntfy_morning"] == ["10:30"] and data["ntfy_quiet_from"] == ["01:00"]
    client.post("/settings/reminders", data=data)
    with session(cfg.db_path) as conn:
        prefs = get_settings(conn)
    assert (prefs["ntfy_ok_meds"], prefs["ntfy_morning"], prefs["ntfy_quiet_from"]) == (str(proc), "10:30", "01:00")


# ============================================================================ Today's shelf and the medicine page


@pytest.fixture()
def clock(monkeypatch, cfg):
    class Frozen(datetime):
        instant = datetime(2026, 5, 6, 12, 0, tzinfo=cfg.tz)  # Day 3, noon

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)

    monkeypatch.setattr(web, "datetime", Frozen)
    return Frozen


def _row(page: str, name: str) -> str:
    shelf = page.split('id="c-prn"')[1].split('class="btn btn-quiet btn-sm shelf-more"')[0]
    return next(chunk for chunk in shelf.split('class="prn-row') if f">{name}</a>" in chunk)


def _turn_on(client, *mids: int) -> None:
    client.post("/settings/reminders", data={**BASE_FORM, "ntfy_ok_meds[]": [str(m) for m in mids],
                                             "ntfy_morning": "09:00", "ntfy_quiet_from": "02:00"})


def test_today_shelf_says_well_tell_you_for_a_ticked_medicine(client, cfg, clock):
    setup_walkthrough(client, DAY1)
    proc = _ids(cfg)["Prochlorperazine"]
    took(cfg, proc, at(D3, 10, 5))
    assert "OK again" not in html.unescape(_row(client.get("/").text, "Prochlorperazine")), "not ticked yet"
    _turn_on(client, proc)
    row = html.unescape(_row(client.get("/").text, "Prochlorperazine"))
    assert "OK at 4:05 PM" in row and "We'll tell you at 4:05 PM" in row
    assert "prn-bell" in row
    with session(cfg.db_path, write=True) as conn:
        put_settings(conn, {"ntfy_enabled": ""})
    assert "tell you" not in html.unescape(_row(client.get("/").text, "Prochlorperazine")), "reminders off"


def test_today_shelf_says_when_an_overnight_ok_time_will_be_told(client, cfg, clock):
    setup_walkthrough(client, DAY1)
    proc = _ids(cfg)["Prochlorperazine"]
    _turn_on(client, proc)
    clock.instant = datetime(2026, 5, 7, 1, 0, tzinfo=cfg.tz)  # Day 4, 1:00 AM
    took(cfg, proc, at(D4, 0, 30))  # OK again before the morning reminder, inside the quiet hours
    row = html.unescape(_row(client.get("/").text, "Prochlorperazine"))
    assert "We'll tell you at 9:00 AM" in row, row


def test_today_shelf_says_nothing_about_later_when_a_ticked_medicine_is_already_ok(client, cfg, clock):
    """'OK to take now' with 'We'll tell you when it's OK again' under it read as a contradiction."""
    setup_walkthrough(client, DAY1)
    proc = _ids(cfg)["Prochlorperazine"]
    _turn_on(client, proc)
    row = html.unescape(_row(client.get("/").text, "Prochlorperazine"))
    assert "OK to take now" in row and "tell you" not in row and "prn-bell" not in row, row


def test_medicine_page_says_whether_the_ok_again_reminder_is_on(client, cfg, clock):
    setup_walkthrough(client, DAY1)
    ids = _ids(cfg)
    proc, dexa = ids["Prochlorperazine"], ids["Dexamethasone"]
    page = html.unescape(client.get(f"/medicines/{proc}").text)
    assert "Reminder when it's OK again" in page and re.search(r'href="/settings#reminders"[^>]*>Off<', page)
    _turn_on(client, proc)
    page = html.unescape(client.get(f"/medicines/{proc}").text)
    assert re.search(r'href="/settings#reminders"[^>]*>On<', page)
    assert "OK again" not in html.unescape(client.get(f"/medicines/{dexa}").text), "scheduled medicines: no line"
