"""Several people, one record: double doses, stale screens, clock changes, and keeping the database safe."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from chemotracker import backup, schedule, store
from chemotracker.config import load_settings
from chemotracker.db import MIGRATIONS, DataDirInUse, connect, lock_data_dir, session
from chemotracker.web import create_app

from .conftest import local_now, setup_walkthrough, today


def _as(client: TestClient, name: str, cfg) -> TestClient:
    """A second phone, signed in as `name`."""
    other = TestClient(client.app)
    with session(cfg.db_path) as conn:
        pid = conn.execute("SELECT id FROM profiles WHERE name = ?", (name,)).fetchone()["id"]
    other.post("/who", data={"profile_id": pid, "next": "/"})
    return other


def test_second_person_is_told_a_scheduled_dose_is_already_recorded(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # John on this phone; today is Day 3
    with session(cfg.db_path) as conn:
        dex = next(s for s in schedule.slots(conn, schedule.cycle_for_date(conn, today(cfg)), 3)
                   if s.plan["med_name"] == "Dexamethasone")
    tap = {"plan_id": dex.plan["id"], "slot": dex.key, "status": "taken", "back": "/"}
    assert client.post("/slot", data=tap, follow_redirects=False).status_code == 303
    jane = _as(client, "Jane", cfg)  # Jane's screen still showed the Taken button
    r = jane.post("/slot", data=tap, follow_redirects=False)
    assert r.status_code == 409 and "Already recorded" in r.text and "John already marked Dexamethasone taken" in r.text
    assert "Don&#39;t give a second dose" in r.text or "Don't give a second dose" in r.text
    with session(cfg.db_path) as conn:
        row = conn.execute("SELECT * FROM dose_logs WHERE plan_id = ?", (dex.plan["id"],)).fetchall()
        john = conn.execute("SELECT id FROM profiles WHERE name = 'John'").fetchone()["id"]
    assert len(row) == 1 and row[0]["logged_by"] == john and row[0]["updated_by"] is None  # untouched
    # Only if she says the entry is wrong does hers replace it.
    assert jane.post("/slot", data={**tap, "confirm": "1"}, follow_redirects=False).status_code == 303
    with session(cfg.db_path) as conn:
        row = conn.execute("SELECT * FROM dose_logs WHERE plan_id = ?", (dex.plan["id"],)).fetchall()
    assert len(row) == 1 and row[0]["updated_by"] != john


def test_as_needed_dose_logged_twice_by_two_people_asks_first(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    with session(cfg.db_path, write=True) as conn:  # a medicine whose label gives no gap
        med = conn.execute("SELECT id FROM medications WHERE name = 'Prochlorperazine'").fetchone()["id"]
        conn.execute("UPDATE medications SET min_hours_between = NULL WHERE id = ?", (med,))
    assert client.post("/dose/quick", data={"medication_id": med, "back": "/"}, follow_redirects=False).status_code == 303
    jane = _as(client, "Jane", cfg)
    r = jane.post("/dose/quick", data={"medication_id": med, "back": "/"}, follow_redirects=False)
    assert r.status_code == 200 and "Check before you log this" in r.text
    assert re.search(r"John logged one at \d+:\d\d [AP]M, less than a minute before this one", r.text)
    assert "logged twice" in r.text
    # The same person tapping again is told it was them.
    r = client.post("/dose/quick", data={"medication_id": med, "back": "/"}, follow_redirects=False)
    assert "You logged one" in r.text
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs WHERE medication_id = ?", (med,)).fetchone()[0] == 1
    assert jane.post("/dose/quick", data={"medication_id": med, "back": "/", "confirm": "1"},
                     follow_redirects=False).status_code == 303
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs WHERE medication_id = ?", (med,)).fetchone()[0] == 2


def test_editing_an_entry_someone_else_just_changed(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT id FROM medications WHERE name = 'Prochlorperazine'").fetchone()["id"]
    client.post("/dose/quick", data={"medication_id": med, "back": "/"})
    with session(cfg.db_path) as conn:
        log_id = conn.execute("SELECT id FROM dose_logs").fetchone()["id"]
    page = client.get(f"/dose/{log_id}").text  # John opens it ...
    seen = re.search(r'name="seen" value="([^"]+)"', page).group(1)
    time.sleep(1.1)  # updated_at has one-second resolution
    jane = _as(client, "Jane", cfg)  # ... Jane saves first
    jseen = re.search(r'name="seen" value="([^"]+)"', jane.get(f"/dose/{log_id}").text).group(1)
    assert jane.post(f"/dose/{log_id}", data={"seen": jseen, "status": "taken", "helped": "yes",
                                              "taken_at": local_now(cfg).strftime("%Y-%m-%dT%H:%M")},
                     follow_redirects=False).status_code == 303
    r = client.post(f"/dose/{log_id}", data={"seen": seen, "status": "taken", "helped": "no",
                                              "taken_at": local_now(cfg).strftime("%Y-%m-%dT%H:%M")}, follow_redirects=False)
    assert r.status_code == 409 and "Jane changed this at" in r.text and "while you had it open" in r.text
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT helped FROM dose_logs WHERE id = ?", (log_id,)).fetchone()["helped"] == "yes"

    # Same for symptoms.
    client.post("/symptoms/new", data={"symptom": "nausea", "severity": "1"})
    with session(cfg.db_path) as conn:
        entry = conn.execute("SELECT id FROM symptom_logs").fetchone()["id"]
    seen = re.search(r'name="seen" value="([^"]+)"', client.get(f"/symptoms/{entry}").text).group(1)
    time.sleep(1.1)
    jseen = re.search(r'name="seen" value="([^"]+)"', jane.get(f"/symptoms/{entry}").text).group(1)
    jane.post(f"/symptoms/{entry}", data={"seen": jseen, "symptom": "nausea", "severity": "3"})
    r = client.post(f"/symptoms/{entry}", data={"seen": seen, "symptom": "nausea", "severity": "0"})
    assert r.status_code == 409 and "Jane changed this" in r.text
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT severity FROM symptom_logs WHERE id = ?", (entry,)).fetchone()["severity"] == 3


def test_gaps_use_real_time_across_clock_changes(cfg):
    la = ZoneInfo("America/Los_Angeles")
    schedule.set_timezone(la)
    try:
        # Spring forward (2:00 AM -> 3:00 AM): 6 real hours after 11 PM is 6 AM, not 5 AM.
        assert schedule.real_add(datetime(2027, 3, 13, 23, 0), timedelta(hours=6)) == datetime(2027, 3, 14, 6, 0)
        # Fall back (2:00 AM -> 1:00 AM): 6 real hours after 11 PM is 4 AM.
        assert schedule.real_add(datetime(2027, 11, 6, 23, 0), timedelta(hours=6)) == datetime(2027, 11, 7, 4, 0)
        assert schedule.real_diff(datetime(2027, 3, 14, 5, 30), datetime(2027, 3, 13, 23, 0)) == timedelta(hours=5, minutes=30)
        with session(cfg.db_path, write=True) as conn:
            med = store.create_medication(conn, {"name": "Example rescue", "route": "oral", "min_hours_between": "6"})
            store.log_dose(conn, med, datetime(2027, 3, 13, 23, 0), None, None)
            m = store.medication(conn, med)
            # 5:30 AM looks like 6.5 hours later on the clock, but only 5.5 real hours have passed.
            warning = schedule.early_dose_warning(conn, m, datetime(2027, 3, 14, 5, 30))
            assert warning and "5 hr 30 min" in warning and "6 hours" in warning
            assert schedule.early_dose_warning(conn, m, datetime(2027, 3, 14, 6, 0)) is None
    finally:
        schedule.set_timezone(cfg.tz)


def test_writers_never_interleave(cfg):
    """Eight people saving at once: a check-then-insert can't produce two rows, and nobody gets an error."""
    with session(cfg.db_path, write=True) as conn:
        med = store.create_medication(conn, {"name": "Example", "route": "oral"})
    errors = []

    def worker():
        try:
            with session(cfg.db_path, write=True) as conn:
                n = conn.execute("SELECT COUNT(*) FROM dose_logs WHERE medication_id = ?", (med,)).fetchone()[0]
                time.sleep(0.02)  # widen the race window
                if n == 0:
                    store.log_dose(conn, med, datetime(2026, 1, 1, 9, 0), None, None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs").fetchone()[0] == 1


def test_a_series_of_cycles_is_all_or_nothing(cfg):
    with session(cfg.db_path, write=True) as conn:
        store.create_cycle(conn, {"number": "4", "day1": "2026-02-01"})
        with pytest.raises(store.InvalidInput):
            store.create_cycle_series(conn, {"number": "2", "day1": "2026-01-04", "length_days": "7", "count": "4"})
    with session(cfg.db_path) as conn:
        assert [r[0] for r in conn.execute("SELECT number FROM cycles ORDER BY number")] == [4]


def test_backups_are_checked_and_never_half_written(cfg, monkeypatch):
    result = backup.run_backup(cfg, "manual")
    assert result.ok and result.path.exists() and result.path.with_suffix(".xlsx").exists()
    assert not list(cfg.backup_dir.glob("*.partial"))
    copy = sqlite3.connect(result.path)
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()
    # A copy that fails its check is thrown away and the failure is recorded.
    monkeypatch.setattr(backup, "quick_check", lambda path: "database disk image is malformed")
    bad = backup.run_backup(cfg, "manual")
    assert not bad.ok and "failed its check" in bad.error
    assert len(backup.list_backups(cfg.backup_dir)) == 1 and not list(cfg.backup_dir.glob("*.partial"))


def test_database_is_copied_before_an_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("CHEMO_TIMEZONE", "America/Los_Angeles")
    settings = load_settings(tmp_path / "data", tmp_path / "backups")
    conn = connect(settings.db_path)
    conn.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")  # an older install
    conn.close()
    create_app(settings, start_scheduler=False)
    copies = list(settings.backup_dir.glob("chemotracker-*-before-upgrade-v1.sqlite3"))
    assert len(copies) == 1
    old = sqlite3.connect(copies[0])
    assert old.execute("PRAGMA user_version").fetchone()[0] == 1
    old.close()
    # A fresh install has nothing to copy.
    fresh = load_settings(tmp_path / "fresh", tmp_path / "fresh-backups")
    create_app(fresh, start_scheduler=False)
    assert not fresh.backup_dir.exists() or not list(fresh.backup_dir.iterdir())


def test_two_servers_cannot_share_a_database(tmp_path):
    first = lock_data_dir(tmp_path)
    with pytest.raises(DataDirInUse):
        lock_data_dir(tmp_path)
    first.close()
    lock_data_dir(tmp_path).close()


def test_startup_check_reports_damage(client, cfg):
    setup_walkthrough(client, today(cfg))
    assert client.app.state.db_health == "ok"
    assert "Database check" not in client.get("/settings").text
