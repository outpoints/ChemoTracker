"""Change history, restoring backups, phone reminders, and getting a medicine ready ahead."""

from __future__ import annotations

import io
import json
import re
from datetime import date, datetime, timedelta

import openpyxl
import pytest

from chemotracker import backup, reminders, rxparse, schedule, store
from chemotracker.config import load_settings
from chemotracker.db import MIGRATIONS, connect, get_settings, migrate, put_settings, session
from chemotracker.web import create_app

from .conftest import setup_walkthrough, today

DAY1 = date(2026, 3, 2)  # a fixed, made-up cycle so reminder times don't depend on the clock


def _history(cfg, entity=None):
    with session(cfg.db_path) as conn:
        sql = "SELECT * FROM history" + (" WHERE entity = ?" if entity else "") + " ORDER BY id"
        return conn.execute(sql, (entity,) if entity else ()).fetchall()


def _ids(cfg):
    with session(cfg.db_path) as conn:
        people = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM profiles")}
        meds = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM medications")}
    return people, meds


# ---------------------------------------------------------------- change history

def test_every_change_is_recorded_with_who_and_what(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    people, meds = _ids(cfg)
    client.post("/dose/quick", data={"medication_id": meds["Prochlorperazine"], "back": "/"})
    with session(cfg.db_path) as conn:
        log_id = conn.execute("SELECT id FROM dose_logs ORDER BY id DESC").fetchone()["id"]
        seen = conn.execute("SELECT updated_at FROM dose_logs WHERE id = ?", (log_id,)).fetchone()[0]
    added = [h for h in _history(cfg, "dose") if h["ref"] == str(log_id)]
    assert added[-1]["action"] == "created" and added[-1]["actor"] == people["John"]

    client.post(f"/dose/{log_id}", data={"seen": seen, "status": "taken", "helped": "yes", "dose_feel": "too_strong",
                                         "taken_at": "2026-01-01T09:15"})
    changed = [h for h in _history(cfg, "dose") if h["ref"] == str(log_id)][-1]
    assert changed["action"] == "changed"
    assert json.loads(changed["before"])["helped"] is None and json.loads(changed["after"])["helped"] == "yes"

    page = client.get(f"/dose/{log_id}").text
    assert "Change history" in page and "John" in page and "Did it help" in page and "Too strong" in page

    # Saving without changing anything isn't a change.
    before = len(_history(cfg))
    with session(cfg.db_path, write=True) as conn:
        conn.execute("UPDATE dose_logs SET updated_at = updated_at WHERE id = ?", (log_id,))
    assert len(_history(cfg)) == before

    # Delete, then put it back from the history page.
    client.post(f"/dose/{log_id}/delete", data={"back": "/"})
    deleted = [h for h in _history(cfg, "dose") if h["ref"] == str(log_id)][-1]
    assert deleted["action"] == "deleted"
    page = client.get("/history").text
    assert "deleted" in page and "Prochlorperazine" in page and f"/history/{deleted['id']}/put-back" in page
    r = client.post(f"/history/{deleted['id']}/put-back", data={"back": "/history"}, follow_redirects=False)
    assert r.headers["location"] == f"/dose/{log_id}"
    with session(cfg.db_path) as conn:
        back = conn.execute("SELECT * FROM dose_logs WHERE id = ?", (log_id,)).fetchone()
    assert back["helped"] == "yes" and back["logged_by"] == people["John"]
    r = client.post(f"/history/{deleted['id']}/put-back", data={"back": "/history"}, follow_redirects=True)
    assert "already back" in r.text


def test_cascades_and_secrets_in_history(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    _, meds = _ids(cfg)
    client.post("/dose/quick", data={"medication_id": meds["Prochlorperazine"], "back": "/"})
    client.post(f"/medicines/{meds['Prochlorperazine']}/delete")
    actions = {(h["entity"], h["action"]) for h in _history(cfg)}
    assert {("medication", "deleted"), ("dose", "deleted"), ("plan", "deleted")} <= actions
    # A deleted medicine's doses can't come back on their own.
    dose_del = [h for h in _history(cfg, "dose") if h["action"] == "deleted"][-1]
    r = client.post(f"/history/{dose_del['id']}/put-back", data={"back": "/history"}, follow_redirects=True)
    assert "medicine was removed" in r.text

    client.post("/settings/reminders", data={"ntfy_enabled": "1", "ntfy_topic": "chemotracker-test1",
                                             "ntfy_token": "tk_example_secret", "ntfy_server": "https://ntfy.sh"})
    page = client.get("/history").text
    assert page.count("Reminder settings") == 1 and "Reminder topic" in page and "chemotracker-test1" in page
    assert "tk_example_secret" not in page
    token_rows = [h for h in _history(cfg, "setting") if h["ref"] == "ntfy_token"]
    assert token_rows and all("tk_example_secret" not in (h["after"] or "") + (h["before"] or "") for h in token_rows)
    with session(cfg.db_path) as conn:
        assert get_settings(conn)["ntfy_token"] == "tk_example_secret"


def test_change_history_sheet_in_the_report(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    _, meds = _ids(cfg)
    client.post("/dose/quick", data={"medication_id": meds["Prochlorperazine"], "back": "/"})
    wb = openpyxl.load_workbook(io.BytesIO(client.get("/report.xlsx").content))
    ws = wb["Change history"]
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=5)]
    assert any(r[1] == "John" and r[2] == "Added" and "Prochlorperazine" in r[3] for r in rows)
    med_head = [c.value for c in wb["Medicines"][4]]
    assert "Get it ready" in med_head


# ---------------------------------------------------------------- restore

def test_restore_a_backup_and_switch_back(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    _, meds = _ids(cfg)
    saved = backup.run_backup(cfg, "manual")
    assert saved.ok and saved.path
    client.post("/dose/quick", data={"medication_id": meds["Prochlorperazine"], "back": "/"})  # after the backup

    settings = client.get("/settings").text
    assert f"/settings/restore?file={saved.path.name}" in settings
    page = client.get(f"/settings/restore?file={saved.path.name}").text
    assert "Everything goes back to how it was then" in page
    assert re.search(r"Doses logged</th><td>0</td><td>1</td>", page)

    r = client.post("/settings/restore", data={"file": saved.path.name}, follow_redirects=True)
    assert "Restored the backup from" in r.text and "before-restore" in r.text
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs").fetchone()[0] == 0
    assert _history(cfg, "database")[-1]["action"] == "restored"
    assert client.app.state.db_health == "ok"

    # The record from just before the restore can be brought back the same way.
    safety = next(f for f in backup.backup_files(cfg) if f.kind == "Saved before a restore")
    client.post("/settings/restore", data={"file": safety.name})
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs").fetchone()[0] == 1


def test_restore_rejects_bad_files(client, cfg):
    setup_walkthrough(client, today(cfg))
    r = client.get("/settings/restore?file=../data/chemotracker.sqlite3", follow_redirects=True)
    assert "a ChemoTracker backup file" in r.text
    r = client.post("/settings/restore/upload", files={"file": ("notes.sqlite3", b"hello, not a database")},
                    follow_redirects=True)
    assert "a ChemoTracker backup" in r.text
    assert not list(cfg.backup_dir.glob("*.partial"))
    # A real copy from "Download a copy" is accepted and goes to the check page.
    data = client.get("/settings/database.sqlite3").content
    r = client.post("/settings/restore/upload", files={"file": ("copy.sqlite3", data)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/restore?file=chemotracker-")
    assert "Uploaded" in client.get(r.headers["location"]).text


# ---------------------------------------------------------------- prep

def test_filgrastim_is_taken_out_of_the_fridge_30_minutes_before(cfg):
    with session(cfg.db_path, write=True) as conn:
        store.create_cycle(conn, {"number": "1", "day1": DAY1.isoformat(), "length_days": "14"})
        med = store.add_from_catalog(conn, "filgrastim")  # Days 2-8 at 3:00 PM by default
        m = store.medication(conn, med)
        assert m["prep_minutes"] == 30 and m["prep_note"] == "Take it out of the fridge"
        slot = next(s for s in schedule.slots(conn, schedule.cycle_for_date(conn, DAY1), 2) if s.plan["medication_id"] == med)
    shot = slot.dt
    assert slot.prep_at == shot - timedelta(minutes=30)
    assert slot.prep_text(shot - timedelta(hours=2)) == "Take it out of the fridge at 2:30 PM"
    assert slot.prep_text(shot - timedelta(minutes=20)) == "Take it out of the fridge now"
    assert slot.prep_text(shot + timedelta(hours=3)) == ""


def test_prep_is_editable_and_checked(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    _, meds = _ids(cfg)
    page = client.get(f"/medicines/{meds['Filgrastim']}").text
    assert "Get it ready" in page and "Take it out of the fridge, 30 minutes before each dose" in page
    base = {"name": "Filgrastim", "route": "injection"}
    r = client.post(f"/medicines/{meds['Filgrastim']}", data={**base, "prep_note": "Take it out", "prep_minutes": ""},
                    follow_redirects=True)
    assert "how many minutes before" in r.text
    client.post(f"/medicines/{meds['Filgrastim']}", data={**base, "prep_note": "", "prep_minutes": ""})
    with session(cfg.db_path) as conn:
        assert store.medication(conn, meds["Filgrastim"])["prep_minutes"] is None  # cleared (e.g. on-body injector)


def test_upgrade_turns_on_prep_for_existing_booster_shots(tmp_path, monkeypatch):
    monkeypatch.setenv("CHEMO_TIMEZONE", "America/Los_Angeles")
    settings = load_settings(tmp_path / "data", tmp_path / "backups")
    conn = connect(settings.db_path)
    script = "".join(MIGRATIONS[:3])
    conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = 3;\n"
                       "INSERT INTO medications (name, info_key, route, created_at, updated_at) "
                       "VALUES ('Filgrastim', 'filgrastim', 'injection', 'x', 'x'), ('Senna', 'senna', 'oral', 'x', 'x');\nCOMMIT;")
    conn.close()
    migrate(settings.db_path)
    with session(settings.db_path) as c:
        rows = {r["name"]: r["prep_minutes"] for r in c.execute("SELECT name, prep_minutes FROM medications")}
    assert rows == {"Filgrastim": 30, "Senna": None}


def test_label_wording_about_the_fridge_is_read():
    [med] = rxparse.parse("Filgrastim 300 mcg/0.5 mL syringe - Inject 300 mcg under the skin once daily. "
                          "Remove from refrigerator 30 minutes before use.")
    assert med.prep_minutes == 30 and med.prep_note == "Take it out of the fridge"


# ---------------------------------------------------------------- reminders

@pytest.fixture()
def remind_db(cfg):
    with session(cfg.db_path, write=True) as conn:
        store.create_cycle(conn, {"number": "1", "day1": DAY1.isoformat(), "length_days": "14"})
        store.create_cycle(conn, {"number": "2", "day1": (DAY1 + timedelta(days=14)).isoformat(), "length_days": "14"})
        fil = store.add_from_catalog(conn, "filgrastim")
        put_settings(conn, {"ntfy_enabled": "1", "ntfy_topic": "chemotracker-test", "app_url": "http://192.0.2.10:8000"})
    return cfg, fil


def _due(cfg, now, **prefs):
    with session(cfg.db_path) as conn:
        p = {**get_settings(conn), **prefs}
        return reminders.due_reminders(conn, now, p)


def test_reminders_prep_due_nag_and_chemo(remind_db):
    cfg, _ = remind_db
    shot = datetime.combine(DAY1 + timedelta(days=1), datetime.min.time()).replace(hour=15)
    [prep] = _due(cfg, shot - timedelta(minutes=29))
    assert prep.key.startswith("prep|") and prep.title == "Take it out of the fridge: Filgrastim"
    assert "The dose is at 3:00 PM" in prep.message and prep.click == "http://192.0.2.10:8000"
    due = [r for r in _due(cfg, shot) if r.key.startswith("due|")]
    assert len(due) == 1 and due[0].title == "Filgrastim at 3:00 PM"
    assert [r.key[:4] for r in _due(cfg, shot + timedelta(minutes=31))] == ["nag|"]
    assert _due(cfg, shot + timedelta(hours=3)) == []  # too old: dropped, not sent late
    # Remind 15 minutes early.
    assert any(r.key.startswith("due|") for r in _due(cfg, shot - timedelta(minutes=15), ntfy_lead="15"))
    # Evening before the next cycle's chemo day.
    eve = datetime.combine(DAY1 + timedelta(days=13), datetime.min.time()).replace(hour=18, minute=5)
    assert any(r.key == f"chemo|{(DAY1 + timedelta(days=14)).isoformat()}" for r in _due(cfg, eve))
    # Private mode keeps medicine names off the ntfy server.
    private = _due(cfg, shot, ntfy_names="0")
    assert all("Filgrastim" not in r.title + r.message for r in private)


def test_reminders_stop_once_the_dose_is_marked(remind_db):
    cfg, fil = remind_db
    shot = datetime.combine(DAY1 + timedelta(days=1), datetime.min.time()).replace(hour=15)
    with session(cfg.db_path, write=True) as conn:
        plan = store.plans(conn, fil)[0]
        store.log_slot(conn, plan, f"{shot:%Y-%m-%d %H:%M}", "taken", shot, None)
    assert _due(cfg, shot + timedelta(minutes=31)) == []


def test_reminder_scheduler_sends_once_and_survives_failures(remind_db, monkeypatch):
    cfg, _ = remind_db
    shot = datetime.combine(DAY1 + timedelta(days=1), datetime.min.time()).replace(hour=15)
    nudger = reminders.ReminderScheduler(cfg)
    monkeypatch.setattr(nudger, "now", lambda: shot)
    sent = []
    assert nudger.tick(lambda prefs, r: sent.append(r.key)) == 1
    assert nudger.tick(lambda prefs, r: sent.append(r.key)) == 0  # never twice
    assert sent == [f"due|{shot:%Y-%m-%d %H:%M}"]

    def broken(prefs, r):
        raise RuntimeError("couldn't reach https://ntfy.sh")
    monkeypatch.setattr(nudger, "now", lambda: shot + timedelta(minutes=31))
    assert nudger.tick(broken) == 0 and "couldn't reach" in nudger.last_error
    assert nudger.tick(lambda prefs, r: sent.append(r.key)) == 1  # retried on the next pass
    with session(cfg.db_path, write=True) as conn:
        put_settings(conn, {"ntfy_enabled": ""})
    assert nudger.tick(lambda prefs, r: sent.append(r.key)) == 0


def test_reminder_settings_form(client, cfg, monkeypatch):
    setup_walkthrough(client, today(cfg))
    page = client.get("/settings").text
    assert "Reminders on your phone" in page and re.search(r'value="chemotracker-[a-z0-9]{6,}"', page)
    r = client.post("/settings/reminders", data={"ntfy_enabled": "1", "ntfy_topic": "bad topic!"}, follow_redirects=True)
    assert "letters, numbers" in r.text
    client.post("/settings/reminders", data={"ntfy_enabled": "1", "ntfy_topic": "chemotracker-abc123",
                                             "ntfy_server": "https://ntfy.sh", "ntfy_lead": "15", "ntfy_nag": "60",
                                             "ntfy_names": "1", "app_url": "http://192.0.2.10:8000"})
    with session(cfg.db_path) as conn:
        p = get_settings(conn)
    assert (p["ntfy_enabled"], p["ntfy_lead"], p["ntfy_nag"], p["ntfy_chemo"]) == ("1", "15", "60", "0")
    calls = []
    monkeypatch.setattr(reminders, "send", lambda prefs, r, timeout=10: calls.append((prefs["ntfy_topic"], r.title)))
    r = client.post("/settings/reminders/test", follow_redirects=True)
    assert "Test sent to chemotracker-abc123" in r.text and calls == [("chemotracker-abc123", "ChemoTracker test")]


def test_send_builds_an_ntfy_json_message(monkeypatch):
    captured = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        captured["url"], captured["body"], captured["auth"] = req.full_url, json.loads(req.data), req.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(reminders.urllib.request, "urlopen", fake_urlopen)
    r = reminders.Reminder("k", "Filgrastim at 3:00 PM", "Filgrastim 1 syringe", ("pill",), 4, "http://192.0.2.10:8000")
    reminders.send({"ntfy_topic": "chemotracker-x", "ntfy_server": "https://ntfy.example.com/", "ntfy_token": "tk_x"}, r)
    assert captured["url"] == "https://ntfy.example.com" and captured["auth"] == "Bearer tk_x"
    assert captured["body"]["topic"] == "chemotracker-x" and captured["body"]["click"] == "http://192.0.2.10:8000"
    assert captured["body"]["actions"][0]["action"] == "view"
