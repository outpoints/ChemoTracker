"""Audit reproductions for as-needed (rescue) medicine timing.

Every test here FAILS on commit 624de25 for the reason in its docstring. Each one names the finding in
audit-findings/asneeded.md. Things that were checked and are fine live in
tests/test_audit_asneeded_ok.py.

The app's clock (web.now) is frozen at an exact instant so time-of-day and daylight-saving cases are
deterministic. Deployed time zone is America/Los_Angeles (see conftest.cfg).
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from chemotracker import schedule, store, web
from chemotracker.db import session
from tests.conftest import setup_walkthrough

LA = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------- helpers


def freeze(monkeypatch, instant: datetime) -> None:
    """Make web.now() return `instant` (an aware datetime). UTC instants pick which 1:30 AM it is on DST night."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.astimezone(LA).replace(tzinfo=None)

    monkeypatch.setattr(web, "datetime", Frozen)


def local(y, mo, d, h, mi=0) -> datetime:
    """An unambiguous Los Angeles wall-clock time as an aware datetime."""
    return datetime(y, mo, d, h, mi, tzinfo=LA)


def med(cfg, name: str):
    with session(cfg.db_path) as c:
        return c.execute("SELECT * FROM medications WHERE name = ?", (name,)).fetchone()


def prn_plan_id(cfg, med_id: int) -> int:
    with session(cfg.db_path) as c:
        return c.execute("SELECT id FROM plans WHERE medication_id = ? AND kind = 'as_needed'", (med_id,)).fetchone()[0]


def write(cfg, sql: str, args=()) -> None:
    with session(cfg.db_path, write=True) as c:
        c.execute(sql, args)


def log(cfg, name: str, when: str) -> int:
    """A taken dose at local wall-clock `when` ('YYYY-MM-DD HH:MM'), stored exactly like Took one stores it."""
    m = med(cfg, name)
    with session(cfg.db_path, write=True) as c:
        return store.log_dose(c, m["id"], datetime.fromisoformat(when), m["dose"], None, prn_plan_id(cfg, m["id"]))


def prn_row(page: str, name: str) -> str:
    """The unescaped HTML of one as-needed row (countdown ring, status, Took one form)."""
    for chunk in page.split('<div class="prn-row')[1:]:
        chunk = chunk.split("shelf-more")[0].split("</section>")[0]
        if f">{name}<" in chunk:
            return html.unescape(chunk)
    raise AssertionError(f"no as-needed row for {name}")


def status(row: str) -> str:
    m = re.search(r"<strong data-status>(.*?)</strong> <span data-wait>(.*?)</span>", row, re.S)
    assert m, row
    return f"{m.group(1)} {m.group(2)}".strip()


# Walkthrough: Prochlorperazine as needed Days 1-6 every 6 h; Ondansetron as needed Days 4-6 every 8 h.


# ---------------------------------------------------------------- F1 daily limit is per calendar day, not 24 hours


def test_f1_daily_limit_resets_at_midnight_instead_of_counting_24_hours(client, cfg, monkeypatch):
    """F1 (P0). "Up to 4 in 24 hours" with doses at 8 AM, noon, 4 PM and 8 PM: at 12:05 AM the app says
    "OK to take now" and Took one logs a 5th dose within 24 hours without any warning, because both the
    shelf (_as_needed_row) and the logging check (early_dose_warning) count `substr(taken_at,1,10) = today`."""
    freeze(monkeypatch, local(2026, 10, 14, 0, 5))
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    write(cfg, "UPDATE medications SET min_hours_between = 4, max_per_day = 4 WHERE name = 'Prochlorperazine'")
    for t in ("08:00", "12:00", "16:00", "20:00"):
        log(cfg, "Prochlorperazine", f"2026-10-13 {t}")
    row = prn_row(client.get("/").text, "Prochlorperazine")
    with session(cfg.db_path) as c:
        warning = schedule.dose_warning(c, med(cfg, "Prochlorperazine"), datetime(2026, 10, 14, 0, 5))
    assert "OK to take now" not in row and warning is not None, (status(row), warning)


# ---------------------------------------------------------------- F2 fall-back night: naive wall-clock comparison


def test_f2_fall_back_night_says_ok_an_hour_early(client, cfg, monkeypatch):
    """F2 (P0, one night a year). Last dose 8:30 PM PDT Oct 31, every 6 hours, so the next is OK at 1:30 AM PST
    (the SECOND 1:30). At the first 1:30 AM (PDT, 08:30 UTC) only 5 real hours have passed, but
    AsNeeded.available compares naive wall-clock times (01:30 >= 01:30) and says "OK to take now"."""
    freeze(monkeypatch, datetime(2026, 11, 1, 8, 30, tzinfo=UTC))  # 1:30 AM PDT, before the clocks go back
    setup_walkthrough(client, datetime(2026, 10, 30).date())
    log(cfg, "Prochlorperazine", "2026-10-31 20:30")
    row = prn_row(client.get("/").text, "Prochlorperazine")
    assert "OK to take now" not in row, status(row)


def test_f3_countdown_ring_runs_out_an_hour_early_across_fall_back(client, cfg, monkeypatch):
    """F3 (P0, one night a year). At midnight PDT with the last dose at 9 PM, the next is OK at 2:00 AM PST:
    3 real hours away (the page itself says "in 3 hr"). The ring's data-remaining and its "2h" label use
    naive wall-clock subtraction (_ui.html countdown, web.short_wait), so app.js flips the row to
    "OK to take now" at 1:00 AM PST, an hour before the server would."""
    freeze(monkeypatch, datetime(2026, 11, 1, 7, 0, tzinfo=UTC))  # 00:00 PDT
    setup_walkthrough(client, datetime(2026, 10, 30).date())
    log(cfg, "Prochlorperazine", "2026-10-31 21:00")
    row = prn_row(client.get("/").text, "Prochlorperazine")
    remaining = int(re.search(r'data-remaining="(-?\d+)"', row).group(1))
    label = re.search(r"<text[^>]*data-left>(.*?)</text>", row).group(1)
    assert "in 3 hr" in status(row)  # the server's own words
    assert (remaining, label) == (3 * 3600, "3h"), (remaining, label, status(row))


def test_f4_dose_logged_in_repeated_hour_is_assumed_to_be_the_earlier_one(monkeypatch):
    """F4 (P0, one night a year). A dose stored as "2026-11-01 01:30" may be the second 1:30 (PST). The app
    can't tell, and real_add/_utc read it as the first (PDT, fold=0), so next_allowed comes out an hour early instead of
    the safe 7:30 AM: an hour early if it was the second 1:30."""
    monkeypatch.setattr(schedule, "_TZ", LA)
    an = schedule.AsNeeded(1, "Prochlorperazine", None, None, None, None, None,
                           last={"taken_at": "2026-11-01 01:30"}, count_today=1, gap_hours=6, max_per_day=None)
    assert an.next_allowed >= datetime(2026, 11, 1, 7, 30), an.next_allowed


# ---------------------------------------------------------------- F5/F6 doses timed after "now" are invisible


def test_f5_dose_logged_a_few_minutes_ahead_shows_ok_right_after_logging(client, cfg, monkeypatch):
    """F5 (P1). Took one -> Other time accepts up to 5 minutes ahead (clock drift). The dose is saved at 10:04,
    but last_taken only looks at `taken_at <= now`, so the page you land on says "OK to take now" for the
    medicine just logged."""
    freeze(monkeypatch, local(2026, 10, 14, 10, 0))
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    m = med(cfg, "Prochlorperazine")
    r = client.post("/dose/quick", data={"medication_id": m["id"], "plan_id": prn_plan_id(cfg, m["id"]),
                                          "ago": "pick", "pick_time": "10:04", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as c:
        assert [x[0] for x in c.execute("SELECT taken_at FROM dose_logs WHERE medication_id = ?", (m["id"],))] \
            == ["2026-10-14 10:04"]
    row = prn_row(client.get("/").text, "Prochlorperazine")
    assert "OK to take now" not in row, status(row)


def test_f6_editing_a_dose_time_into_the_future_hides_it(client, cfg, monkeypatch):
    """F6 (P1). The dose page (store.update_dose_log) saves any time, even later today (a typo such as 11:00
    for 9:00). Every logging route refuses future times, the edit doesn't, and the edited dose then drops
    out of last_taken, so the shelf says "OK to take now" one hour after a dose."""
    freeze(monkeypatch, local(2026, 10, 14, 10, 0))
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    log_id = log(cfg, "Prochlorperazine", "2026-10-14 09:00")
    r = client.post(f"/dose/{log_id}", data={"status": "taken", "taken_at": "2026-10-14T11:00", "dose": "1 tablet",
                                             "back": "/"}, follow_redirects=False)
    row = prn_row(client.get("/").text, "Prochlorperazine")
    assert "OK to take now" not in row, (r.status_code, status(row))


# ---------------------------------------------------------------- F7/F8 days the as-needed plan doesn't cover


def test_f7_log_a_dose_says_ok_on_a_day_the_plan_does_not_allow(client, cfg, monkeypatch):
    """F7 (P1). Ondansetron is as needed Days 4-6 only. On Day 7 the shelf and symptom page say "Not planned
    today ... check with your care team", but "Log a dose" (log_dose_context -> _as_needed_row, which
    never sets scheduled_today) says "OK to take now", and saving gives no warning."""
    freeze(monkeypatch, local(2026, 10, 18, 10, 0))  # Day 7
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    log(cfg, "Ondansetron", "2026-10-17 08:00")
    m = med(cfg, "Ondansetron")
    page = html.unescape(client.get(f"/dose/new?med={m['id']}").text)
    option = re.search(rf'<option value="{m["id"]}"[^>]*>', page).group(0)
    assert "OK to take now" not in option, option


def test_f8_yesterdays_page_offers_ok_for_a_medicine_not_allowed_today(client, cfg, monkeypatch):
    """F8 (P1). On Day 7, opening Day 6 on Today (/?d=) shows Ondansetron's row computed for NOW
    (as_needed_for_day keeps scheduled_today=True because the plan covered Day 6): "OK to take now" and a
    Took one button that logs the dose today, a day the plan doesn't allow it."""
    freeze(monkeypatch, local(2026, 10, 18, 10, 0))  # Day 7
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    log(cfg, "Ondansetron", "2026-10-17 08:00")
    row = prn_row(client.get("/?d=2026-10-17").text, "Ondansetron")
    assert "OK to take now" not in row, status(row)


def test_f9_last_day_promises_ok_tomorrow_when_tomorrow_is_not_planned(client, cfg, monkeypatch):
    """F9 (P2). Day 6 is Ondansetron's last as-needed day. At 9 PM after an 8 PM dose the row says
    "OK tomorrow at 4:00 AM", and an open page's app.js countdown turns it into "OK to take now" at 4 AM on
    Day 7, which the schedule doesn't allow."""
    freeze(monkeypatch, local(2026, 10, 17, 21, 0))  # Day 6
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    log(cfg, "Ondansetron", "2026-10-17 20:00")
    row = prn_row(client.get("/").text, "Ondansetron")
    assert "OK tomorrow" not in row and "data-remaining" not in row, status(row)


# ---------------------------------------------------------------- F10 daily limit reached, but "in 3 hr"


def test_f10_daily_limit_row_still_counts_down_to_the_gap(client, cfg, monkeypatch):
    """F10 (P1). Up to 2 a day, every 4 hours, doses at 8 AM and noon. At 1 PM the row reads
    "Reached 2 today in 3 hr": wait_text ignores the daily limit, so it reads as OK again at 4 PM.
    Later in the day it becomes "Reached 2 today in less than a minute"."""
    freeze(monkeypatch, local(2026, 10, 14, 13, 0))
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    write(cfg, "UPDATE medications SET min_hours_between = 4, max_per_day = 2 WHERE name = 'Prochlorperazine'")
    log(cfg, "Prochlorperazine", "2026-10-14 08:00")
    log(cfg, "Prochlorperazine", "2026-10-14 12:00")
    row = prn_row(client.get("/").text, "Prochlorperazine")
    assert status(row) == "Reached 2 today", status(row)


# ---------------------------------------------------------------- F11 "Other time" ahead of now becomes yesterday


def test_f11_other_time_slightly_ahead_is_silently_logged_as_yesterday(client, cfg, monkeypatch):
    """F11 (P2). At 10:00 AM, Took one -> Other time 10:20 (a slip of the clock wheel) is saved as YESTERDAY
    10:20 (dose_time: "a time later than now means last night", for any hour). The dose just taken then
    doesn't count today and the row says "OK to take now"."""
    freeze(monkeypatch, local(2026, 10, 14, 10, 0))
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    m = med(cfg, "Prochlorperazine")
    client.post("/dose/quick", data={"medication_id": m["id"], "plan_id": prn_plan_id(cfg, m["id"]),
                                     "ago": "pick", "pick_time": "10:20", "back": "/"}, follow_redirects=False)
    with session(cfg.db_path) as c:
        saved = [x[0] for x in c.execute("SELECT taken_at FROM dose_logs WHERE medication_id = ?", (m["id"],))]
    assert "2026-10-13 10:20" not in saved, (saved, status(prn_row(client.get("/").text, "Prochlorperazine")))


# ---------------------------------------------------------------- F12 fractional gaps shown rounded


def test_f12_fractional_gap_is_shown_rounded(client, cfg, monkeypatch):
    """F12 (P3). "Every 4.25 hours" (saved as 4.25) is shown as "every 4.2 hr" (Jinja round(1)), which
    understates the wait. The countdown itself uses the exact value."""
    freeze(monkeypatch, local(2026, 10, 14, 10, 0))
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    write(cfg, "UPDATE medications SET min_hours_between = 4.25 WHERE name = 'Prochlorperazine'")
    row = prn_row(client.get("/").text, "Prochlorperazine")
    assert "every 4.2 hr" not in row, re.search(r"every [^·<]*", row).group(0)


# ---------------------------------------------------------------- F13 "every day of the cycle" hidden on delay days


def test_f13_every_day_as_needed_medicine_is_blocked_when_chemo_is_delayed(client, cfg, monkeypatch):
    """F13 (P2). An as-needed medicine saved for "Every day of the cycle" (end_day NULL) vanishes from Today and
    is called "Not planned today (Every day of the cycle)" on Day 16 of a 14-day cycle, i.e. whenever the next
    chemo is delayed (common with low counts) and on every Day 0. plan_end() stops open-ended plans at
    length_days."""
    freeze(monkeypatch, local(2026, 10, 27, 10, 0))  # Day 16 of a 14-day cycle, next cycle not added yet
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    write(cfg, """UPDATE plans SET end_day = NULL WHERE kind = 'as_needed'
                   AND medication_id = (SELECT id FROM medications WHERE name = 'Prochlorperazine')""")
    r = client.post("/symptoms/new", data={"symptom": "nausea", "severity": "2"})
    row = prn_row(client.get(r.url.path).text, "Prochlorperazine")
    assert not status(row).startswith("Not planned today"), status(row)


# ---------------------------------------------------------------- F14 follow-up question drops the day


def test_f14_follow_up_question_says_which_day(client, cfg, monkeypatch):
    """F14 (P3). At 8 AM the follow-up fold-out asks "How did the Prochlorperazine at 9:00 PM go?" for a dose
    taken YESTERDAY at 9 PM (today.html:13 prints fmt_time only), which reads like a dose still to come."""
    freeze(monkeypatch, local(2026, 10, 14, 8, 0))
    setup_walkthrough(client, datetime(2026, 10, 12).date())
    log(cfg, "Prochlorperazine", "2026-10-13 21:00")
    q = re.search(r'<div class="q">(.*?)</div>', html.unescape(client.get("/").text)).group(1)
    assert "yesterday" in q.lower(), q


# ---------------------------------------------------------------- F15 milligram daily limits read as dose counts


def test_f15_prescription_import_turns_a_milligram_daily_limit_into_a_dose_count():
    """F15 (P0). Pasting a label with a daily limit in mg sets max_per_day from the wrong units
    (rxparse.parse_medicine, rxparse.py:831-838): "2 tablets every 4 to 6 hours, do not exceed 3000 mg per day"
    of a 500 mg tablet becomes "up to 1500 a day" (label: 3 doses), so with the 4-hour gap the shelf says OK to a
    4th, 5th and 6th dose (6000 mg). "2 tablets (8 mg) ... max 24 mg per day" becomes 12 doses (label: 3), and
    "1 tablet ... do not exceed 40 mg per day" of a 10 mg tablet becomes 40 doses (label: 4)."""
    from chemotracker import rxparse

    labels = {
        "ACETAMINOPHEN 500 MG TABLET\nTake 2 tablets by mouth every 4 to 6 hours as needed for pain. "
        "Do not exceed 3000 mg per day.": 3,
        "ONDANSETRON 4 MG TABLET\nTake 2 tablets (8 mg) by mouth every 8 hours as needed for nausea. Max 24 mg per day.": 3,
        "PROCHLORPERAZINE 10 MG TABLET\nTake 1 tablet by mouth every 6 hours as needed for nausea. "
        "Do not exceed 40 mg per day.": 4,
    }
    got = {text.split()[0]: rxparse.parse(text)[0].max_per_day for text in labels}
    want = {text.split()[0]: n for text, n in labels.items()}
    assert all(got[k] in (None, want[k]) for k in got), (got, want)
