from __future__ import annotations

import io
import re
from datetime import date, timedelta

import openpyxl

from chemotracker import backup, profiles, schedule, store
from chemotracker.db import get_settings
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


def test_password_is_optional(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from chemotracker.config import load_settings
    from chemotracker.web import create_app

    # Unset, empty, or blank all mean no sign-in screen.
    for value in (None, "", "   "):
        if value is None:
            monkeypatch.delenv("CHEMO_PASSWORD", raising=False)
        else:
            monkeypatch.setenv("CHEMO_PASSWORD", value)
        cfg = load_settings(tmp_path / f"d{len(value or 'x')}", tmp_path / "b")
        assert cfg.password is None
        with TestClient(create_app(cfg, start_scheduler=False)) as c:
            assert c.get("/", follow_redirects=False).headers["location"] == "/setup"
            assert c.get("/login", follow_redirects=False).status_code == 303


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
                "/manifest.webmanifest", "/who", "/people", "/setup/people"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:500])
    assert client.get("/medicines/9999").status_code == 404


def test_entries_record_who_logged_them(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    with session(cfg.db_path) as conn:
        people = {p["name"]: p["id"] for p in conn.execute("SELECT * FROM profiles")}
        cycle = schedule.cycle_for_date(conn, today(cfg))
        dex = next(s for s in schedule.slots(conn, cycle, 3) if s.plan["med_name"] == "Dexamethasone")
    assert set(people) == {"John", "Jane"}
    # John (this device) logs a dose.
    client.post("/slot", data={"plan_id": dex.plan["id"], "slot": dex.key, "status": "taken", "back": "/"})
    # Jane switches in on the same device, logs a symptom, and answers the dose follow-up.
    r = client.post("/who", data={"profile_id": people["Jane"], "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/symptoms/new", data={"symptom": "fatigue", "severity": "1"})
    with session(cfg.db_path) as conn:
        log = conn.execute("SELECT * FROM dose_logs WHERE plan_id = ?", (dex.plan["id"],)).fetchone()
        sym = conn.execute("SELECT * FROM symptom_logs").fetchone()
    assert log["logged_by"] == people["John"] and log["updated_by"] is None
    assert sym["logged_by"] == people["Jane"]
    client.post(f"/dose/{log['id']}/follow", data={"helped": "yes", "dose_feel": "right", "back": "/"})
    with session(cfg.db_path) as conn:
        log = store.dose_log(conn, log["id"])
    assert log["logged_by"] == people["John"] and log["updated_by"] == people["Jane"]

    page = client.get("/").text
    assert 'class="by"' in page and "John" in page and "edited by Jane" in page
    assert "Tired · Mild" in page and "Jane" in client.get("/symptoms").text

    wb = openpyxl.load_workbook(io.BytesIO(client.get(f"/report.xlsx?cycle={cycle['id']}").content))
    dl = wb["Dose log"]
    assert dl.cell(row=4, column=16).value == "Logged by"
    by_values = [dl.cell(row=i, column=16).value for i in range(5, dl.max_row + 1)]
    assert "John (edited by Jane)" in by_values
    sy = wb["Symptoms"]
    assert sy.cell(row=4, column=9).value == "Logged by" and sy.cell(row=5, column=9).value == "Jane"
    summary = [c.value for row in wb["Summary"].iter_rows() for c in row if c.value]
    assert "Who logged entries" in summary
    csv_text = client.get(f"/report-doses.csv?cycle={cycle['id']}").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].endswith(",Logged by") and "John (edited by Jane)" in csv_text


def test_new_device_is_asked_who_it_is(cfg):
    from fastapi.testclient import TestClient

    from chemotracker.web import create_app

    app = create_app(cfg, start_scheduler=False)
    with TestClient(app) as first:
        setup_walkthrough(first, today(cfg))
    with TestClient(app) as second:  # a different phone: no cookie yet
        r = second.get("/symptoms", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/who?next=%2Fsymptoms")
        page = second.get("/who").text
        assert "John" in page and "Jane" in page
        with session(cfg.db_path) as conn:
            jane = conn.execute("SELECT id FROM profiles WHERE name = 'Jane'").fetchone()["id"]
        r = second.post("/who", data={"profile_id": jane, "next": "/symptoms"}, follow_redirects=False)
        assert r.headers["location"] == "/symptoms"
        assert second.get("/symptoms").status_code == 200
        assert "Using as" in second.get("/").text


def test_people_can_be_added_renamed_and_removed(client, cfg):
    setup_walkthrough(client, today(cfg))
    client.post("/people", data={"name": "Sam", "role": "caregiver"})
    r = client.post("/people", data={"name": "sam", "role": "family"}, follow_redirects=True)
    assert "already someone called" in r.text
    with session(cfg.db_path) as conn:
        sam = conn.execute("SELECT * FROM profiles WHERE name = 'Sam'").fetchone()
        med_id = conn.execute("SELECT id FROM medications LIMIT 1").fetchone()[0]
    assert sam["role"] == "caregiver"
    client.post("/who", data={"profile_id": sam["id"], "next": "/"})
    client.post("/dose/quick", data={"medication_id": med_id, "back": "/"})
    client.post(f"/people/{sam['id']}", data={"name": "Sam R", "role": "caregiver"})
    client.post(f"/people/{sam['id']}/active", data={"active": "0"})
    # Removed people keep their name on past entries, and this device must pick again.
    with session(cfg.db_path) as conn:
        assert profiles.name_of(profiles.by_id(conn), conn.execute(
            "SELECT logged_by FROM dose_logs").fetchone()[0]) == "Sam R"
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"].startswith("/who")
    page = client.get("/people").text
    assert "Removed" in page and "Sam R" in page


def test_single_person_is_selected_automatically(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        jane = conn.execute("SELECT id FROM profiles WHERE name = 'Jane'").fetchone()["id"]
        profiles.set_active(conn, jane, False)
    client.cookies.clear()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200 and "John" in r.text


def test_v1_database_upgrades_in_place(tmp_path):
    import sqlite3

    from chemotracker.db import MIGRATIONS, migrate

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
    conn.execute("INSERT INTO medications (name, created_at, updated_at) VALUES ('X', 'now', 'now')")
    conn.execute("INSERT INTO dose_logs (medication_id, status, taken_at, created_at, updated_at) "
                 "VALUES (1, 'taken', '2026-01-01 08:00', 'now', 'now')")
    conn.commit()
    conn.close()
    assert migrate(path) == len(MIGRATIONS)
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dose_logs)")}
    assert {"logged_by", "updated_by"} <= cols
    assert conn.execute("SELECT logged_by FROM dose_logs").fetchone()[0] is None
    conn.close()


def test_install_without_people_prompts_to_add_them(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        conn.execute("UPDATE dose_logs SET logged_by = NULL")
        conn.execute("DELETE FROM profiles")
    client.cookies.clear()
    page = client.get("/")
    assert page.status_code == 200 and "Add who's using ChemoTracker" in page.text


def _setup_until_medicines(client, day1, **cycle):
    client.post("/setup/people", data={"names[]": ["John"], "roles[0]": "patient", "me": "new:0"})
    data = {"number": "1", "day1": day1.isoformat(), "length_days": "14", "count": "4", "regimen": "AC", **cycle}
    r = client.post("/setup/cycle", data=data, follow_redirects=False)
    assert r.headers["location"] == "/setup/medicines", r.text


def test_setup_reads_pasted_prescriptions(client, cfg):
    from chemotracker.web import RX_EXAMPLE

    day1 = today(cfg)
    _setup_until_medicines(client, day1)
    page = client.get("/setup/medicines").text
    assert "Write in your prescriptions" in page and "See an example" in page and "OLANZapine 5MG TAB NORT" in page

    preview = client.post("/rx/preview", data={"rx_text": RX_EXAMPLE})
    assert preview.status_code == 200
    html = preview.text
    assert 'data-keys="ondansetron,prochlorperazine,dexamethasone,olanzapine"' in html
    assert "Days 2–4" in html and schedule.fmt_day(day1 + timedelta(days=1)) in html  # real dates from chemo day
    assert "at least 8 hours apart" in html and "guess" in html

    # No-JS path: "Read it" re-renders the page with the preview and ticks the matches.
    r = client.post("/setup/medicines", data={"rx_text": RX_EXAMPLE, "action": "preview"})
    assert r.status_code == 200 and "Found 4 medicines" in r.text
    assert 'value="olanzapine" checked' in r.text

    # Save with olanzapine unticked in the preview, plus one ticked from the list.
    r = client.post("/setup/medicines", data={"rx_text": RX_EXAMPLE, "rx_seen": "1", "rx_include[]": ["0", "1", "2"],
                                              "keys[]": ["doxorubicin"]}, follow_redirects=False)
    assert r.headers["location"] == "/setup/label/0"
    with session(cfg.db_path) as conn:
        meds = {m["info_key"]: m for m in store.medications(conn)}
        assert set(meds) == {"ondansetron", "prochlorperazine", "dexamethasone", "doxorubicin"}
        dex = meds["dexamethasone"]
        assert (dex["strength"], dex["dose"]) == ("4 mg tablet", "2 tablets (8 mg)")
        assert [(p["kind"], p["start_day"], p["end_day"], p["times"]) for p in store.plans(conn, dex["id"])] == \
            [("scheduled", 2, 4, "08:00")]
        assert meds["ondansetron"]["min_hours_between"] == 8
        # The pasted text itself is never stored.
        dump = " ".join(str(v) for row in conn.execute("SELECT * FROM medications") for v in tuple(row))
        assert "NORT" not in dump


def test_two_part_regimen_and_cycle_scoped_prescriptions(client, cfg):
    day1 = today(cfg)
    _setup_until_medicines(client, day1, then_regimen="Paclitaxel", then_count="12", then_length_days="7")
    with session(cfg.db_path) as conn:
        cycles = schedule.all_cycles(conn)
    assert [c["number"] for c in cycles] == list(range(1, 17))
    assert cycles[3]["day1"] == (day1 + timedelta(days=42)).isoformat()
    assert cycles[4]["day1"] == (day1 + timedelta(days=56)).isoformat() and cycles[4]["length_days"] == 7
    assert cycles[15]["regimen"] == "Paclitaxel"
    text = "Olanzapine 5 mg tab - Take 1 tablet at bedtime on days 1 to 4"
    html = client.post("/rx/preview", data={"rx_text": text}).text
    assert "AC · cycles 1–4" in html and "Paclitaxel · cycles 5–16" in html
    client.post("/setup/medicines", data={"rx_text": text, "rx_seen": "1", "rx_include[]": ["0"], "rx_cycles_0": "1-4"})
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT id FROM medications WHERE info_key = 'olanzapine'").fetchone()
        plan = store.plans(conn, med["id"])[0]
    assert (plan["from_cycle"], plan["to_cycle"]) == (1, 4)


def test_add_medicine_and_fill_from_label(client, cfg):
    setup_walkthrough(client, today(cfg))
    r = client.post("/medicines/add", data={"rx_text": "Lorazepam 0.5 mg tab - Take 1 tablet by mouth every 6 hours as needed for anxiety"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/medicines/")
    with session(cfg.db_path) as conn:
        lor = conn.execute("SELECT * FROM medications WHERE info_key = 'lorazepam'").fetchone()
        olz = conn.execute("SELECT * FROM medications WHERE info_key = 'olanzapine'").fetchone()
    assert lor["min_hours_between"] == 6 and lor["strength"] == "0.5 mg tablet"
    r = client.post(f"/medicines/{olz['id']}/import",
                    data={"rx_text": "Olanzapine 10 mg tab - Take 1 tablet by mouth at 8 PM on days 1 to 3"}, follow_redirects=True)
    assert "Filled in from the label" in r.text
    with session(cfg.db_path) as conn:
        olz = store.medication(conn, olz["id"])
        plans = store.plans(conn, olz["id"])
    assert olz["strength"] == "10 mg tablet" and [(p["start_day"], p["end_day"], p["times"]) for p in plans] == [(1, 3, "20:00")]


def test_taper_doses_show_on_today(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))  # today is Day 2
    text = "Dexamethasone 4 mg tab - Take 1 tablet the evening of chemo, then 2 tablets twice daily for 3 days"
    with session(cfg.db_path) as conn:
        dex = conn.execute("SELECT id FROM medications WHERE info_key = 'dexamethasone'").fetchone()["id"]
    client.post(f"/medicines/{dex}/import", data={"rx_text": text})
    html = client.get("/").text
    assert "2 tablets (8 mg)" in html
    with session(cfg.db_path) as conn:
        day1 = [s for s in schedule.slots(conn, schedule.all_cycles(conn)[0], 1) if s.plan["medication_id"] == dex]
    assert [(s.time, s.dose) for s in day1] == [("18:00", "1 tablet (4 mg)")]


def test_as_needed_too_soon_asks_first(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # Day 3, prochlorperazine every 6 h
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT id FROM medications WHERE info_key = 'prochlorperazine'").fetchone()["id"]
    client.post("/dose/quick", data={"medication_id": med, "back": "/"})
    r = client.post("/dose/quick", data={"medication_id": med, "back": "/"})
    assert r.status_code == 200 and "Check before you log this" in r.text and "6 hours" in r.text
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs WHERE medication_id = ?", (med,)).fetchone()[0] == 1
    r = client.post("/dose/quick", data={"medication_id": med, "back": "/", "confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs WHERE medication_id = ?", (med,)).fetchone()[0] == 2
    future = (local_now(cfg) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    r = client.post("/dose/new", data={"medication_id": med, "taken_at": future})
    assert r.status_code == 400 and "hasn&#39;t happened yet" in r.text


def test_temperature_units(client, cfg):
    setup_walkthrough(client, today(cfg))
    # Default °F; a reading in °C is recognised and converted, with a note.
    r = client.post("/symptoms/new", data={"symptom": "temperature", "value": "38.2"}, follow_redirects=False)
    saved = client.get(r.headers["location"]).text
    assert "100.8\u00a0°F" in saved and "callout-urgent" in saved
    with session(cfg.db_path) as conn:
        row = conn.execute("SELECT * FROM symptom_logs").fetchone()
    assert row["value"] == 100.8 and row["notes"] == "Entered as 38.2 °C."
    # Switch to °C: the fever line is typed in °C and stored in °F, readings show in °C.
    r = client.post("/settings", data={"temp_unit": "C", "fever_line": "38.5", "call_if": "Fever"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        prefs = get_settings(conn)
    assert prefs["temp_unit"] == "C" and float(prefs["fever_f"]) == 101.3
    page = client.get("/symptoms").text
    assert "38.2\u00a0°C" in page and "°F" not in page.split("What are you noticing")[1]
    form = client.get("/symptoms/new?s=temperature").text
    assert "Temperature (°C)" in form and "38.5\u00a0°C" in form
    client.post("/symptoms/new", data={"symptom": "temperature", "value": "37.1"})
    with session(cfg.db_path) as conn:
        latest = conn.execute("SELECT * FROM symptom_logs ORDER BY id DESC").fetchone()
    assert latest["value"] == 98.8 and latest["notes"] is None  # typed in the chosen unit, no note
    r = client.post("/symptoms/new", data={"symptom": "temperature", "value": "12"})
    assert r.status_code == 400 and "body temperature" in r.text
    bad = client.post("/settings", data={"temp_unit": "C", "fever_line": "45"}, follow_redirects=True)
    assert "fever line should be between" in bad.text
    wb = openpyxl.load_workbook(io.BytesIO(client.get("/report.xlsx").content))
    daily = wb["Daily overview"]
    assert daily.cell(row=4, column=8).value == "Highest temp °C"
    assert "38.2\u00a0°C" in [wb["Symptoms"].cell(row=i, column=7).value for i in range(5, wb["Symptoms"].max_row + 1)]


def test_moving_chemo_moves_later_cycles(client, cfg):
    day1 = today(cfg)
    client.get("/")
    client.post("/setup/people", data={"names[]": ["John"], "roles[0]": "patient", "me": "new:0"})
    client.post("/setup/cycle", data={"number": "1", "day1": day1.isoformat(), "length_days": "14", "count": "4"})
    with session(cfg.db_path) as conn:
        c2 = [c for c in schedule.all_cycles(conn) if c["number"] == 2][0]
    page = client.get(f"/cycles/{c2['id']}?change=1").text
    assert "Chemo moved? Change the date" in page and "Move the 2 cycles after this one too" in page
    new = (date.fromisoformat(c2["day1"]) + timedelta(days=7)).isoformat()
    r = client.post(f"/cycles/{c2['id']}", data={"day1": new, "number": "2", "length_days": "14", "shift_later": "1"},
                    follow_redirects=True)
    assert "moved 7 days later too" in r.text
    with session(cfg.db_path) as conn:
        days = {c["number"]: c["day1"] for c in schedule.all_cycles(conn)}
    assert days == {1: day1.isoformat(), 2: new, 3: (day1 + timedelta(days=35)).isoformat(),
                    4: (day1 + timedelta(days=49)).isoformat()}
    # Only this one: later cycles stay put.
    client.post(f"/cycles/{c2['id']}", data={"day1": c2["day1"], "number": "2", "length_days": "14"})
    with session(cfg.db_path) as conn:
        assert {c["number"]: c["day1"] for c in schedule.all_cycles(conn)}[3] == (day1 + timedelta(days=35)).isoformat()


def test_medicines_list_and_page_for_non_technical_users(client, cfg):
    setup_walkthrough(client, today(cfg))
    page = client.get("/medicines").text
    assert "At home" in page and "Given at the clinic" in page
    clinic_part = page.split("Given at the clinic")[1]
    assert "Add the label details" not in clinic_part  # infusions have no bottle to copy
    with session(cfg.db_path) as conn:
        olz = conn.execute("SELECT id FROM medications WHERE info_key = 'olanzapine'").fetchone()["id"]
        dox = conn.execute("SELECT id FROM medications WHERE info_key = 'doxorubicin'").fetchone()["id"]
    detail = client.get(f"/medicines/{olz}").text
    assert "What you take" in detail and "Change details or schedule" in detail
    assert '<details class="edit-zone" id="edit" >' in detail or '<details class="edit-zone" id="edit">' in detail  # folded
    assert "What the clinic gives" in client.get(f"/medicines/{dox}").text
    assert 'id="edit" open' in client.get(f"/medicines/{olz}?edit=1").text


def test_prescription_preview_says_when_it_updates(client, cfg):
    setup_walkthrough(client, today(cfg))
    html = client.post("/rx/preview", data={"rx_text": "Olanzapine 5 mg tab - Take 1 tablet at bedtime"}).text
    assert "Already on your list · saving updates it" in html
    html = client.post("/rx/preview", data={"rx_text": "Lorazepam 0.5 mg tab - Take 1 tablet every 6 hours as needed"}).text
    assert "In the medicine list" in html


def test_rerunning_setup_names_what_it_stops(client, cfg):
    setup_walkthrough(client, today(cfg))
    keys = ["doxorubicin", "cyclophosphamide", "paclitaxel", "ondansetron", "prochlorperazine", "dexamethasone", "olanzapine"]
    r = client.post("/setup/medicines", data={"keys[]": keys}, follow_redirects=True)
    assert "Stopped Filgrastim because it was unticked" in r.text


def test_header_has_a_call_button(client, cfg):
    setup_walkthrough(client, today(cfg))
    assert 'class="icon-link call-link" href="tel:555-0100"' in client.get("/").text


def test_theme_controls(client, cfg):
    setup_walkthrough(client, today(cfg))
    settings = client.get("/settings").text
    # The theme choice comes first in Settings, in plain words.
    assert settings.index("Light or dark</h2>") < settings.index("<h2>Care team</h2>")
    for value, label in (("auto", "Automatic"), ("light", "Light"), ("dark", "Dark")):
        assert f'value="{value}" data-theme-pick>{label}</label>' in settings
    assert "Night" not in settings
    # Desktop sidebar has a one-tap switch. It stays hidden until the script can run it.
    home = client.get("/").text
    assert re.search(r"<button[^>]*data-theme-toggle hidden", home)
    # The saved choice is applied before the page paints, so there is no flash of the wrong theme.
    assert re.search(r'<script src="/static/js/theme\.js\?v=\w+"></script>', home)  # blocking, not deferred
    assert home.index("/static/js/theme.js") < home.index("<body>")


def test_asset_urls_change_with_content(client):
    # Cache-busting uses a content hash, so an update reaches phones that cached the old CSS.
    page = client.get("/settings").text
    m = re.search(r'/static/css/app\.css\?v=([0-9a-f]{10})"', page)
    assert m and f"/static/js/app.js?v={m.group(1)}" in page
