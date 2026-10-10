from __future__ import annotations

import io
import re
from datetime import timedelta

import openpyxl

from chemotracker import backup, schedule, store
from chemotracker.db import session

from .conftest import local_now, setup_walkthrough, today


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_walkthrough_builds_todays_compartments(client, cfg):
    # Make "today" Day 3, the sheet's busiest day.
    day1 = today(cfg) - timedelta(days=2)
    setup_walkthrough(client, day1)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Cycle 1 · Day 3" in html
    for period in ("Morning", "Afternoon", "Bedtime"):
        assert f'<span class="c-name">{period}</span>' in html
    assert "Dexamethasone" in html and "2 tablets (4 mg)" in html
    assert "Filgrastim" in html and "Olanzapine" in html
    assert "As needed" in html and "Prochlorperazine" in html
    # Ondansetron is planned for Days 4-6 only.
    assert "Not on today&#39;s schedule" in html or "Other as-needed medicines" in html
    assert "How are you feeling?" in html


def test_bedtime_label_lands_in_bedtime(conn):
    assert schedule.period_for("07:15") == "morning"
    assert schedule.period_for("14:00") == "afternoon"
    assert schedule.period_for("18:00") == "evening"
    assert schedule.period_for("21:00") == "bedtime"
    assert schedule.period_for("02:00") == "bedtime"
    assert schedule.period_for("19:00", "At bedtime") == "bedtime"


def test_taking_a_dose_is_idempotent_and_undoable(client, cfg):
    day1 = today(cfg) - timedelta(days=2)
    setup_walkthrough(client, day1)
    with session(cfg.db_path) as conn:
        dex = next(s for s in schedule.slots(conn, schedule.cycle_for_date(conn, today(cfg)), 3)
                   if s.plan["med_name"] == "Dexamethasone")
    for _ in range(2):  # double tap
        r = client.post("/slot", data={"plan_id": dex.plan["id"], "slot": dex.key, "status": "taken", "back": "/"},
                        follow_redirects=False)
        assert r.status_code == 303
    with session(cfg.db_path) as conn:
        rows = conn.execute("SELECT * FROM dose_logs WHERE plan_id = ?", (dex.plan["id"],)).fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "taken" and rows[0]["dose"] == "2 tablets (4 mg)"
        log_id = rows[0]["id"]
    page = client.get("/")
    assert "Dexamethasone marked taken" in page.text and f"/dose/{log_id}/undo" in page.text
    client.post(f"/dose/{log_id}/undo", data={"back": "/"})
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs").fetchone()[0] == 0


def test_as_needed_gap_and_follow_up(client, cfg):
    day1 = today(cfg) - timedelta(days=2)
    setup_walkthrough(client, day1)
    with session(cfg.db_path) as conn:
        compazine = conn.execute("SELECT * FROM medications WHERE name = 'Prochlorperazine'").fetchone()
        assert compazine["min_hours_between"] == 6
        two_hours_ago = local_now(cfg) - timedelta(hours=2)
        log_id = store.log_dose(conn, compazine["id"], two_hours_ago, "1 tablet", "nausea")
        an = schedule._as_needed_row(conn, compazine, None, local_now(cfg))
        assert not an.available(local_now(cfg))
        assert an.next_allowed == two_hours_ago + timedelta(hours=6)
        assert 0.3 < an.fraction(local_now(cfg)) < 0.37
    html = client.get("/").text
    assert "OK at" in html or "OK tomorrow at" in html
    assert "How did the Prochlorperazine" in html  # follow-up prompt after 30 min
    r = client.post(f"/dose/{log_id}/follow", data={"helped": "some", "dose_feel": "not_enough", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        row = store.dose_log(conn, log_id)
        assert row["helped"] == "some" and row["dose_feel"] == "not_enough"


def test_symptom_logging_links_to_rescue_meds_and_flags_fever(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=4))  # Day 5: ondansetron + prochlorperazine allowed
    r = client.post("/symptoms/new", data={"symptom": "nausea", "severity": "2", "notes": "after lunch"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = client.get(r.headers["location"]).text
    assert "Nausea · Moderate" in saved
    assert "Your as-needed medicines for nausea" in saved
    assert "Ondansetron" in saved and "Prochlorperazine" in saved
    assert "callout-urgent" not in saved

    r = client.post("/symptoms/new", data={"symptom": "temperature", "value": "100.8"}, follow_redirects=False)
    saved = client.get(r.headers["location"]).text
    assert "callout-urgent" in saved and "555-0100" in saved

    r = client.post("/symptoms/new", data={"symptom": "nausea"}, follow_redirects=False)
    assert r.status_code == 400 and "Choose how bad" in r.text

    page = client.get("/symptoms").text
    assert "Nausea · Moderate" in page and "100.8\u00a0°F" in page
    assert client.get("/").text.count("sev-tag") >= 2


def test_symptom_suggestions_respect_planned_days(client, cfg):
    # Day 3: ondansetron is only planned for Days 4-6, so it must not say "OK to take now".
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    r = client.post("/symptoms/new", data={"symptom": "nausea", "severity": "2"}, follow_redirects=False)
    saved = client.get(r.headers["location"]).text
    ond = saved.split("Ondansetron")[1].split("prn-row")[0]
    assert "Not planned today (Days 4–6)" in ond and "OK to take now" not in ond and "Took one now" not in ond
    comp = saved.split("Prochlorperazine")[1]
    assert "OK to take now" in comp and "Took one now" in comp


def test_medicine_and_plan_are_editable(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        olz = conn.execute("SELECT * FROM medications WHERE name = 'Olanzapine'").fetchone()
        plan = store.plans(conn, olz["id"])[0]
    r = client.post(f"/medicines/{olz['id']}", data={"name": "Olanzapine", "strength": "7.5 mg tablet", "dose": "1 tablet",
                                                    "directions": "Take 1 tablet at bedtime", "route": "oral"},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/plans/{plan['id']}", data={"kind": "scheduled", "days[]": ["1", "2", "3", "4"], "times[]": ["20:30"]},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/plans/{plan['id']}", data={"kind": "scheduled", "days[]": ["2", "4"], "times[]": ["20:30"]},
                    follow_redirects=True)
    assert "next to each other" in r.text
    with session(cfg.db_path) as conn:
        assert store.medication(conn, olz["id"])["strength"] == "7.5 mg tablet"
        p = store.plan(conn, plan["id"])
        assert (p["start_day"], p["end_day"], p["times"]) == (1, 4, "20:30")
    page = client.get(f"/medicines/{olz['id']}").text
    assert "About Olanzapine" in page and "MedlinePlus" in page


def test_add_custom_medicine(client, cfg):
    setup_walkthrough(client, today(cfg))
    r = client.post("/medicines/add", data={"custom_name": "Magic mouthwash"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/medicines/")
    assert "Magic mouthwash" in client.get("/medicines").text


def test_moving_day1_moves_the_schedule(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    with session(cfg.db_path) as conn:
        c = schedule.all_cycles(conn)[0]
    client.post(f"/cycles/{c['id']}", data={"number": "1", "day1": (today(cfg) - timedelta(days=10)).isoformat(),
                                            "length_days": "14"})
    html = client.get("/").text
    assert "Day 11" in html and "Dexamethasone" not in html.split("today-aside")[0].split("feel")[-1]


def test_excel_report_and_csv(client, cfg):
    day1 = today(cfg) - timedelta(days=4)
    setup_walkthrough(client, day1)
    with session(cfg.db_path) as conn:
        cycle = schedule.cycle_for_date(conn, today(cfg))
        dex = next(s for s in schedule.slots(conn, cycle, 2) if s.plan["med_name"] == "Dexamethasone")
        store.log_slot(conn, dex.plan, dex.key, "taken", dex.dt + timedelta(minutes=12), dex.dose)
        store.add_symptom(conn, {"symptom": "nausea", "severity": "3"}, local_now(cfg))
    r = client.get(f"/report.xlsx?cycle={cycle['id']}")
    assert r.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[:4] == ["Summary", "Daily overview", "Dose log", "Symptoms"]
    assert "Cycle 1 grid" in wb.sheetnames and "Medicines" in wb.sheetnames
    log = wb["Dose log"]
    statuses = [log.cell(row=i, column=10).value for i in range(5, log.max_row + 1)]
    assert "Taken" in statuses and "Not logged" in statuses  # gaps are visible
    meds = wb["Medicines"]
    labels = [meds.cell(row=i, column=4).value for i in range(5, meds.max_row + 1)]
    assert "Example: 1 tablet every 8 hours as needed" in labels
    sym = wb["Symptoms"]
    assert sym.cell(row=5, column=5).value == "Nausea" and sym.cell(row=5, column=6).value == "Severe"

    csv_text = client.get(f"/report-doses.csv?cycle={cycle['id']}").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("Date,Time,Planned time,Cycle,Day,Medicine")
    assert re.search(r"Dexamethasone.*Taken", csv_text)
    sym_csv = client.get(f"/report-symptoms.csv?cycle={cycle['id']}").content.decode("utf-8-sig")
    assert "Nausea,Severe" in sym_csv


def test_backup_writes_snapshot_and_prunes(cfg, client):
    setup_walkthrough(client, today(cfg))
    for _ in range(3):
        result = backup.run_backup(cfg, "test")
        assert result.ok and result.path and result.path.exists()
        assert result.path.with_suffix(".xlsx").exists()
    files = backup.list_backups(cfg.backup_dir)
    assert 1 <= len(files) <= cfg.backup_keep
    from dataclasses import replace

    small = replace(cfg, backup_keep=1)
    backup.prune(cfg.backup_dir, small.backup_keep)
    assert len(backup.list_backups(cfg.backup_dir)) == 1
    import sqlite3

    snap = sqlite3.connect(backup.list_backups(cfg.backup_dir)[0])
    assert snap.execute("SELECT COUNT(*) FROM medications").fetchone()[0] == 8
    snap.close()
    page = client.get("/settings").text
    assert "Last good backup" in page and "None yet" not in page


def test_backup_schedule_math(cfg):
    from dataclasses import replace
    from datetime import datetime, time

    daily = replace(cfg, backup_interval_hours=24, backup_time=time(3, 0))
    assert backup.next_run(daily, datetime(2026, 10, 9, 20, 15)) == datetime(2026, 10, 10, 3, 0)
    assert backup.next_run(daily, datetime(2026, 10, 10, 2, 59)) == datetime(2026, 10, 10, 3, 0)
    six = replace(cfg, backup_interval_hours=6, backup_time=time(3, 0))
    assert backup.next_run(six, datetime(2026, 10, 9, 10, 0)) == datetime(2026, 10, 9, 15, 0)
    assert backup.is_overdue(daily, datetime(2026, 10, 9, 20, 0))  # never backed up


def test_password_gate(cfg, monkeypatch):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from chemotracker.web import create_app

    locked = replace(cfg, password="family")
    with TestClient(create_app(locked, start_scheduler=False)) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert c.get("/healthz").status_code == 200
        assert c.post("/login", data={"password": "nope", "next": "/"}).status_code == 401
        r = c.post("/login", data={"password": "family", "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/", follow_redirects=False).headers["location"] == "/setup"


def test_every_page_renders(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    with session(cfg.db_path) as conn:
        med_id = conn.execute("SELECT id FROM medications LIMIT 1").fetchone()[0]
        cycle_id = conn.execute("SELECT id FROM cycles LIMIT 1").fetchone()[0]
        entry = store.add_symptom(conn, {"symptom": "pain", "value": "4", "notes": "back"}, local_now(cfg))
        log = store.log_dose(conn, med_id, local_now(cfg), "1", None)
    for url in ["/", f"/?d={(today(cfg) + timedelta(days=3)).isoformat()}", "/symptoms", "/symptoms/new?s=nausea",
                "/symptoms/new?s=temperature", "/symptoms/new?s=pain", "/symptoms/new?s=vomiting",
                "/symptoms/new?s=other", f"/symptoms/{entry}", f"/symptoms/{entry}/saved", "/medicines",
                "/medicines/add", f"/medicines/{med_id}", "/cycles", f"/cycles/{cycle_id}", "/report",
                "/report?cycle=&start=2026-01-01&end=2026-01-31", "/settings", "/setup", "/setup/medicines",
                "/setup/label/0", "/setup/cycle", "/setup/team", f"/dose/{log}", f"/dose/new?med={med_id}",
                "/manifest.webmanifest"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:500])
    assert client.get("/medicines/9999").status_code == 404
