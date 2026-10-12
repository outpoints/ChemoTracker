"""Plan and setup leftovers from the safety audit: each test reproduces one finding from the audit notes.

Overlapping cycle dates, schedule times after midnight (which night they mean), and the same drug added twice
as separate medicines.
"""

from __future__ import annotations

import html
import re
from datetime import date, timedelta

from chemotracker import schedule, store
from chemotracker.db import session

from .conftest import setup_walkthrough, today


def _plan(conn, med_name, kind="scheduled"):
    return conn.execute("""SELECT p.* FROM plans p JOIN medications m ON m.id = p.medication_id
                            WHERE m.name = ? AND p.kind = ?""", (med_name, kind)).fetchone()


def _med(conn, name):
    return conn.execute("SELECT * FROM medications WHERE name = ?", (name,)).fetchone()


def _flash_after(client, r) -> str:
    """The one-time message a redirect queued, read from the next page."""
    assert r.status_code == 303, r.text
    return html.unescape(client.get(r.headers["location"]).text)


# ---------------------------------------------------------------- 5. cycle dates


def test_cycles_with_the_same_or_overlapping_dates_are_refused(client, cfg):
    d1 = today(cfg) - timedelta(days=20)
    setup_walkthrough(client, d1)  # cycle 1: 14 days

    def add(number, day1, count=1, length=14):
        return client.post("/cycles", data={"number": str(number), "day1": day1.isoformat(), "length_days": str(length),
                                            "count": str(count)}, follow_redirects=False)

    def numbers():
        with session(cfg.db_path) as conn:
            return [c["number"] for c in schedule.all_cycles(conn)]

    text = _flash_after(client, add(2, d1))
    assert "Cycle 1 already has its chemo day (Day 1) on" in text and numbers() == [1]
    text = _flash_after(client, add(2, d1 + timedelta(days=5)))
    assert f"Cycle 1 runs from {schedule.fmt_day(d1)} to {schedule.fmt_day(d1 + timedelta(days=13))}" in text
    assert numbers() == [1]
    _flash_after(client, add(5, d1 + timedelta(days=10), count=3))  # several at once, the first one inside cycle 1
    assert numbers() == [1]
    # Back to back is fine: the next cycle's Day 0 is the last day of the one before.
    assert add(2, d1 + timedelta(days=14), count=2).status_code == 303 and numbers() == [1, 2, 3]
    with session(cfg.db_path) as conn:
        c1, c2, c3 = schedule.all_cycles(conn)

    def edit(c, **values):
        data = {"number": str(c["number"]), "day1": c["day1"], "length_days": str(c["length_days"]), **values}
        return client.post(f"/cycles/{c['id']}", data=data, follow_redirects=False)

    text = _flash_after(client, edit(c1, length_days="21"))  # cycle 2's chemo day would fall inside cycle 1
    assert "Cycle 2's chemo day" in text and numbers() == [1, 2, 3]
    with session(cfg.db_path) as conn:
        assert store.cycle(conn, c1["id"])["length_days"] == 14
    later = (date.fromisoformat(c2["day1"]) + timedelta(days=7)).isoformat()
    _flash_after(client, edit(c2, day1=later))  # a week's delay, leaving cycle 3 where it was: overlaps cycle 3
    with session(cfg.db_path) as conn:
        assert store.cycle(conn, c2["id"])["day1"] == c2["day1"]
    assert edit(c2, day1=later, shift_later="1").status_code == 303  # moving the later ones too is fine
    with session(cfg.db_path) as conn:
        assert [c["day1"] for c in schedule.all_cycles(conn)][1:] == [
            later, (date.fromisoformat(c3["day1"]) + timedelta(days=7)).isoformat()]
    assert edit(c3, regimen="Example").status_code == 303  # other changes still save


def test_first_run_setup_refuses_a_second_part_that_overlaps_the_first(client, cfg):
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/setup"
    client.post("/setup/people", data={"names[]": ["John"], "roles[0]": "patient", "me": "new:0"})
    d1 = today(cfg)
    r = client.post("/setup/cycle", data={"number": "1", "day1": d1.isoformat(), "length_days": "14", "count": "2",
                                          "then_count": "4", "then_length_days": "7", "then_gap": "7"},
                    follow_redirects=False)
    assert r.status_code == 400
    assert "Cycle 2 runs from" in html.unescape(r.text)
    with session(cfg.db_path) as conn:
        assert schedule.all_cycles(conn) == []


# ---------------------------------------------------------------- 6. times after midnight


def test_a_schedule_time_after_midnight_explains_which_night_it_means(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))
    with session(cfg.db_path) as conn:
        olz = _plan(conn, "Olanzapine")
    r = client.post(f"/plans/{olz['id']}", data={"kind": "scheduled", "days[]": ["2", "3", "4"], "times[]": ["00:30"],
                                                 "label": "At bedtime", "from_cycle": "1"}, follow_redirects=False)
    text = _flash_after(client, r)
    assert ("A time after midnight belongs to the start of that day. 12:30 AM on Day 2 is the night between Day 1 "
            "and Day 2. If you meant the night after Day 2, choose Days 3 to 5.") in text
    with session(cfg.db_path) as conn:  # the schedule itself is saved as entered
        assert store.plan(conn, olz["id"])["times"] == "00:30"
    r = client.post(f"/medicines/{olz['medication_id']}/plans", data={"kind": "scheduled", "days[]": ["6"],
                                                                     "times[]": ["02:00"]}, follow_redirects=False)
    assert "2:00 AM on Day 6 is the night between Day 5 and Day 6. If you meant the night after Day 6, choose Day 7." \
        in _flash_after(client, r)
    page = html.unescape(client.get(f"/medicines/{olz['medication_id']}?edit=1").text)
    assert "A time after midnight belongs to the start of that day" in page
    preview = html.unescape(client.post("/rx/preview", data={
        "rx_text": "Olanzapine 5 MG TAB - Take 1 tablet by mouth at 12:30 AM on days 2, 3 and 4"}).text)
    assert "12:30 AM on Day 2 is the night between Day 1 and Day 2" in preview


# ---------------------------------------------------------------- 7. one drug, two medicines


def test_a_custom_medicine_that_is_a_catalog_drug_already_on_the_list_asks_first(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))
    with session(cfg.db_path) as conn:
        pro = _med(conn, "Prochlorperazine")
        before = len(store.medications(conn, active=None))
    r = client.post("/medicines/add", data={"custom_name[]": ["Compazine"]}, follow_redirects=False)
    text = html.unescape(r.text)
    assert r.status_code == 200, r.status_code
    assert "Compazine is another name for Prochlorperazine, which is already on your list." in text
    primary = re.search(r'<a class="btn btn-primary btn-lg" href="([^"]+)">([^<]+)</a>', text)
    assert primary and primary.groups() == (f"/medicines/{pro['id']}", "Open Prochlorperazine")
    with session(cfg.db_path) as conn:
        assert len(store.medications(conn, active=None)) == before
    form = text.split('<form method="post" action="/medicines/add">')[1].split("</form>")[0]
    fields = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)
    replay: dict[str, list[str]] = {}
    for k, v in fields:
        replay.setdefault(k, []).append(html.unescape(v))
    r = client.post("/medicines/add", data=replay, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?new=1"), r.headers["location"]
    with session(cfg.db_path) as conn:
        assert len(store.medications(conn, active=None)) == before + 1
