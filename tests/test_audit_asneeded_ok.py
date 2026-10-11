"""Audit guards: as-needed cases that were checked and behave correctly on 624de25 (all should pass).

Reproductions of real problems are in tests/test_audit_asneeded.py.
"""

from __future__ import annotations

import html
from datetime import datetime

from chemotracker import schedule, store
from chemotracker.db import session
from tests.conftest import setup_walkthrough
from tests.test_audit_asneeded import LA, freeze, local, log, med, prn_plan_id, prn_row, status, write

DAY1 = datetime(2026, 10, 12).date()  # Day 3 = Oct 14, Day 4 = Oct 15


def _an(last: str, gap: float, monkeypatch) -> schedule.AsNeeded:
    monkeypatch.setattr(schedule, "_TZ", LA)
    return schedule.AsNeeded(1, "X", None, None, None, None, None, last={"taken_at": last}, count_today=1,
                             gap_hours=gap, max_per_day=None)


def test_no_gap_saved_never_says_ok(client, cfg, monkeypatch):
    freeze(monkeypatch, local(2026, 10, 14, 10, 0))
    setup_walkthrough(client, DAY1)
    write(cfg, "UPDATE medications SET min_hours_between = NULL WHERE name = 'Prochlorperazine'")
    row = prn_row(client.get("/").text, "Prochlorperazine")
    assert status(row) == "Time between doses not saved Follow the label"
    assert 'class="countdown open"' in row and "tick" not in row and "data-remaining" not in row
    m = med(cfg, "Prochlorperazine")
    page = html.unescape(client.get(f"/dose/new?med={m['id']}").text)
    option = page.split(f'<option value="{m["id"]}"')[1].split(">")[0]
    assert 'data-status="Time between doses not saved"' in option  # (Ondansetron's option says OK on Day 3: F7)


def test_later_logged_earlier_timed_dose_does_not_reset_the_wait(client, cfg, monkeypatch):
    freeze(monkeypatch, local(2026, 10, 14, 13, 0))
    setup_walkthrough(client, DAY1)
    log(cfg, "Prochlorperazine", "2026-10-14 12:00")
    log(cfg, "Prochlorperazine", "2026-10-14 09:00")  # logged second, taken first
    assert status(prn_row(client.get("/").text, "Prochlorperazine")) == "OK at 6:00 PM in 5 hr"


def test_skipped_dose_does_not_count(client, cfg, monkeypatch):
    freeze(monkeypatch, local(2026, 10, 14, 10, 0))
    setup_walkthrough(client, DAY1)
    log_id = log(cfg, "Prochlorperazine", "2026-10-14 09:00")
    client.post(f"/dose/{log_id}", data={"status": "skipped", "back": "/"})
    assert status(prn_row(client.get("/").text, "Prochlorperazine")) == "OK to take now"


def test_scheduled_dose_of_the_same_medicine_counts_toward_gap_and_limit(client, cfg, monkeypatch):
    freeze(monkeypatch, local(2026, 10, 14, 10, 0))
    setup_walkthrough(client, DAY1)
    m = med(cfg, "Prochlorperazine")
    write(cfg, "UPDATE medications SET max_per_day = 1 WHERE id = ?", (m["id"],))
    with session(cfg.db_path, write=True) as c:
        pid = store.create_plan(c, m["id"], {"kind": "scheduled", "start_day": 1, "end_day": 6, "times": "08:00",
                                             "label": "", "dose": ""})
        store.log_slot(c, store.plan(c, pid), "2026-10-14 08:00", "taken", datetime(2026, 10, 14, 8, 0), None)
    assert status(prn_row(client.get("/").text, "Prochlorperazine")).startswith("Reached 1 today")


def test_spring_forward_gap_uses_real_time(monkeypatch):
    an = _an("2027-03-13 23:00", 6, monkeypatch)
    assert an.next_allowed == datetime(2027, 3, 14, 6, 0)  # 23:00 PST + 6 real hours = 06:00 PDT
    assert not an.available(datetime(2027, 3, 14, 5, 30)) and an.state(datetime(2027, 3, 14, 6, 0)) == "ok"


def test_fall_back_gap_outside_the_repeated_hour_is_right(monkeypatch):
    an = _an("2026-10-31 22:00", 6, monkeypatch)
    assert an.next_allowed == datetime(2026, 11, 1, 3, 0)  # 22:00 PDT + 6 real hours = 03:00 PST
    assert an.state(datetime(2026, 11, 1, 2, 30)) == "wait"


def test_fractional_gap_is_exact(monkeypatch):
    an = _an("2026-10-14 08:00", 4.5, monkeypatch)
    assert an.next_allowed == datetime(2026, 10, 14, 12, 30)
    assert an.state(datetime(2026, 10, 14, 12, 29)) == "wait"


def test_plan_starting_tomorrow_is_not_offered_today(client, cfg, monkeypatch):
    freeze(monkeypatch, local(2026, 10, 14, 10, 0))  # Day 3; Ondansetron is Days 4-6
    setup_walkthrough(client, DAY1)
    assert ">Ondansetron<" not in client.get("/").text.split('id="c-prn"')[1].split("</details>")[0]
    r = client.post("/symptoms/new", data={"symptom": "nausea", "severity": "2"})
    row = prn_row(client.get(r.url.path).text, "Ondansetron")
    assert status(row).startswith("Not planned today (Days 4") and "/dose/quick" not in row


def test_dose_given_the_day_before_the_plan_starts_still_blocks(client, cfg, monkeypatch):
    freeze(monkeypatch, local(2026, 10, 15, 2, 0))  # Day 4, 2 AM
    setup_walkthrough(client, DAY1)
    log(cfg, "Ondansetron", "2026-10-14 23:00")  # Day 3, not a planned day
    assert status(prn_row(client.get("/").text, "Ondansetron")) == "OK at 7:00 AM in 5 hr"


def test_daily_limit_without_gap_never_says_ok(client, cfg, monkeypatch):
    freeze(monkeypatch, local(2026, 10, 14, 13, 0))
    setup_walkthrough(client, DAY1)
    write(cfg, "UPDATE medications SET min_hours_between = NULL, max_per_day = 2 WHERE name = 'Prochlorperazine'")
    log(cfg, "Prochlorperazine", "2026-10-14 08:00")
    assert status(prn_row(client.get("/").text, "Prochlorperazine")) == "Time between doses not saved Follow the label"
    log(cfg, "Prochlorperazine", "2026-10-14 12:00")
    assert status(prn_row(client.get("/").text, "Prochlorperazine")) == "Reached 2 today"


def test_took_one_at_an_earlier_time_too_close_to_a_logged_dose_asks_first(client, cfg, monkeypatch):
    freeze(monkeypatch, local(2026, 10, 14, 15, 0))
    setup_walkthrough(client, DAY1)
    log(cfg, "Prochlorperazine", "2026-10-14 08:00")
    m = med(cfg, "Prochlorperazine")
    r = client.post("/dose/quick", data={"medication_id": m["id"], "plan_id": prn_plan_id(cfg, m["id"]), "ago": "pick",
                                         "pick_time": "12:00", "back": "/"}, follow_redirects=False)
    assert r.status_code == 200 and "at least 6 hours between doses" in html.unescape(r.text)
