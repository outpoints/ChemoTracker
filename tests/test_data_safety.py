"""Data-safety guards from the integrity audit: ids are never reused (v5), typed text never runs as a formula in
the clinic's files, Put back never records one dose twice, and a restore says what it removes and keeps the way
back. Demo people only (John, Jane).
"""

from __future__ import annotations

import csv
import html
import io
import re
import sqlite3
from datetime import timedelta

import openpyxl
import pytest

from chemotracker import backup, db, export, store
from chemotracker.db import connect, migrate, session

from .conftest import local_now, setup_walkthrough, today

TABLES = ("medications", "plans", "cycles", "dose_logs", "symptom_logs")
T0 = "2026-01-01T00:00:00+00:00"


def _v4(path, monkeypatch):
    """A database as v0.5.1 left it (schema v4), with entries, deletions and their history."""
    full = db.MIGRATIONS
    monkeypatch.setattr(db, "MIGRATIONS", full[:4])
    assert migrate(path) == 4
    with session(path, write=True) as conn:
        conn.execute("INSERT INTO profiles (name, role, color, created_at) VALUES ('John', 'patient', '#11616a', ?)", (T0,))
        for name in ("Dexamethasone", "Olanzapine", "Ondansetron"):
            conn.execute("INSERT INTO medications (name, dose, notes, created_at, updated_at) VALUES (?, '1 tablet', "
                         "'=1+2', ?, ?)", (name, T0, T0))
        conn.execute("INSERT INTO plans (medication_id, kind, start_day, end_day, times, created_at) "
                     "VALUES (1, 'scheduled', 2, 4, '08:00', ?)", (T0,))
        conn.execute("INSERT INTO plans (medication_id, kind, start_day, end_day, times, created_at) "
                     "VALUES (3, 'as_needed', 1, 6, '', ?)", (T0,))
        conn.execute("INSERT INTO cycles (number, day1, created_at) VALUES (1, '2026-01-01', ?)", (T0,))
        for i, (plan, slot, status) in enumerate([(1, "2026-01-02 08:00", "taken"), (1, "2026-01-03 08:00", "skipped"),
                                                   (None, None, "taken"), (1, "2026-01-04 08:00", "taken")]):
            conn.execute("INSERT INTO dose_logs (medication_id, plan_id, kind, slot, status, taken_at, created_at, "
                         "updated_at, logged_by) VALUES (1, ?, 'scheduled', ?, ?, ?, ?, ?, 1)",
                         (plan, slot, status, None if status == "skipped" else f"2026-01-0{i + 2} 08:05", T0, T0))
        conn.execute("INSERT INTO symptom_logs (logged_at, symptom, severity, value, notes, created_at, updated_at) "
                     "VALUES ('2026-01-02 09:00', 'nausea', 2, 2.5, '@x', ?, ?)", (T0, T0))
        conn.execute("INSERT INTO symptom_logs (logged_at, symptom, severity, created_at, updated_at) "
                     "VALUES ('2026-01-02 10:00', 'fatigue', 1, ?, ?)", (T0, T0))
        conn.execute("DELETE FROM dose_logs WHERE id = 4")      # the newest dose: v0.5.1 would hand id 4 out again
        conn.execute("DELETE FROM symptom_logs WHERE id = 2")
        conn.execute("DELETE FROM medications WHERE id = 3")    # cascades its plan
    monkeypatch.setattr(db, "MIGRATIONS", full)


def _dump(path):
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in TABLES}, \
            {t: conn.execute(f"PRAGMA table_info({t})").fetchall() for t in TABLES}
    finally:
        conn.close()


def test_v5_upgrade_copies_every_row_and_ids_are_never_handed_out_twice(tmp_path, monkeypatch):
    path = tmp_path / "v4.sqlite3"
    _v4(path, monkeypatch)
    rows, columns = _dump(path)
    assert migrate(path) == 5
    after_rows, after_columns = _dump(path)
    assert after_rows == rows, "the upgrade changed or lost an entry"
    assert after_columns == columns, "v0.5.1 reads these columns by name and order; they must not change"
    assert db.quick_check(path) == "ok"
    with session(path, write=True) as conn:
        new_dose = db.rowid(conn.execute("INSERT INTO dose_logs (medication_id, kind, status, taken_at, created_at, "
                                         "updated_at) VALUES (1, 'as_needed', 'taken', '2026-01-05 10:00', ?, ?)", (T0, T0)))
        new_med = db.rowid(conn.execute("INSERT INTO medications (name, created_at, updated_at) VALUES ('Pegfilgrastim', ?, ?)",
                                        (T0, T0)))
        new_sym = db.rowid(conn.execute("INSERT INTO symptom_logs (logged_at, symptom, created_at, updated_at) "
                                        "VALUES ('2026-01-05 10:00', 'pain', ?, ?)", (T0, T0)))
        assert (new_dose, new_med, new_sym) == (5, 4, 3), "a deleted entry's id was handed to a new one"
        # Still one row per planned dose, and removing a medicine still takes its schedule with it.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO dose_logs (medication_id, plan_id, slot, status, created_at, updated_at) "
                         "VALUES (1, 1, '2026-01-02 08:00', 'taken', ?, ?)", (T0, T0))
        conn.execute("INSERT INTO plans (medication_id, kind, start_day, created_at) VALUES (?, 'clinic', 1, ?)", (new_med, T0))
        conn.execute("DELETE FROM medications WHERE id = ?", (new_med,))
        assert not conn.execute("SELECT 1 FROM plans WHERE medication_id = ?", (new_med,)).fetchone()
    assert migrate(path) == 5  # running it again changes nothing


def test_v5_upgrade_changes_nothing_if_a_table_holds_something_unexpected(tmp_path, monkeypatch):
    path = tmp_path / "v4.sqlite3"
    _v4(path, monkeypatch)
    conn = connect(path)
    conn.execute("ALTER TABLE dose_logs ADD COLUMN extra TEXT")
    conn.execute("UPDATE dose_logs SET extra = 'kept'")
    conn.close()
    rows, _ = _dump(path)
    with pytest.raises(sqlite3.Error):
        migrate(path)
    assert db.schema_version(path) == 4
    assert _dump(path)[0] == rows, "a failed upgrade must leave the record exactly as it was"


def test_formula_like_text_is_plain_text_on_every_sheet_and_in_both_csvs(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path, write=True) as conn:
        conn.execute("UPDATE profiles SET name = '=HYPERLINK(\"http://example.invalid\",\"Jane\")' WHERE name = 'Jane'")
        conn.execute("UPDATE medications SET directions = '+1 tablet', notes = '-2' WHERE name = 'Dexamethasone'")
        conn.execute("UPDATE settings SET value = '@John' WHERE key = 'patient_name'")
        plan = conn.execute("SELECT p.* FROM plans p JOIN medications m ON m.id = p.medication_id "
                            "WHERE m.name = 'Dexamethasone'").fetchone()
        med = plan["medication_id"]
        conn.execute("INSERT INTO dose_logs (medication_id, plan_id, kind, slot, status, taken_at, notes, created_at, "
                     "updated_at) VALUES (?, ?, 'scheduled', ?, 'taken', ?, '=1+2', ?, ?)",
                     (med, plan["id"], f"{y} 08:00", f"{y} 08:05", T0, T0))
        conn.execute("INSERT INTO symptom_logs (logged_at, symptom, label, severity, notes, created_at, updated_at) "
                     "VALUES (?, 'other', '=2+3', 2, '\t=cmd', ?, ?)", (f"{y} 09:00", T0, T0))
    wb = openpyxl.load_workbook(io.BytesIO(client.get(f"/report.xlsx?start={y}&end={y}").content))
    cells = [c for ws in wb.worksheets for row in ws.iter_rows() for c in row]
    assert not [c.coordinate for c in cells if c.data_type == "f"]
    texts = {c.value for c in cells if isinstance(c.value, str)}
    assert "=1+2" in texts and any(t.startswith("=2+3") for t in texts) and "+1 tablet" in texts
    assert all(c.quotePrefix for c in cells if isinstance(c.value, str) and c.value and c.value[0] in "=+-@\t\r")
    day = next(c for c in wb["Dose log"]["E"] if isinstance(c.value, int))
    assert day.data_type == "n", "numbers stay numbers"
    doses = list(csv.reader(io.StringIO(client.get(f"/report-doses.csv?start={y}&end={y}").content.decode("utf-8-sig"))))
    syms = list(csv.reader(io.StringIO(client.get(f"/report-symptoms.csv?start={y}&end={y}").content.decode("utf-8-sig"))))
    fields = [f for row in doses[1:] + syms[1:] for f in row]
    assert "'=1+2" in fields and "'=2+3" in fields and "'\t=cmd" in fields
    assert not [f for f in fields if f and f[0] in "=+-@\t\r"]
    assert any(row[doses[0].index("Day")] == "2" for row in doses[1:])


def _plan(conn, name):
    return conn.execute("SELECT p.* FROM plans p JOIN medications m ON m.id = p.medication_id WHERE m.name = ?",
                        (name,)).fetchone()


def test_put_back_names_a_deleted_medicines_doses_by_their_own_name_in_the_report(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        fil = _plan(conn, "Filgrastim")
    client.post("/slot", data={"plan_id": fil["id"], "slot": f"{y} 15:00", "status": "taken", "taken_at": f"{y}T15:05",
                               "back": "/"})
    r = client.post(f"/medicines/{fil['medication_id']}/delete", follow_redirects=True)
    assert "1 dose in the record" in html.unescape(r.text)
    with session(cfg.db_path) as conn:
        rows = export.dose_rows(conn, y, y, local_now(cfg))
    assert [r.status for r in rows if r.medicine.startswith("Filgrastim")] == ["Taken"]


def test_put_back_after_a_time_change_asks_first_with_leave_it_as_it_is_primary(client, cfg):
    """The deleted dose's planned time no longer exists, and that day's one dose was logged at the new time."""
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    y = today(cfg) - timedelta(days=1)
    with session(cfg.db_path) as conn:
        dex = _plan(conn, "Dexamethasone")
    client.post("/slot", data={"plan_id": dex["id"], "slot": f"{y} 08:00", "status": "taken", "taken_at": f"{y}T08:05",
                               "back": "/"})
    with session(cfg.db_path) as conn:
        first = conn.execute("SELECT id FROM dose_logs WHERE plan_id = ?", (dex["id"],)).fetchone()["id"]
    client.post(f"/dose/{first}/delete", data={"back": "/"})
    r = client.post(f"/plans/{dex['id']}", data={"kind": "scheduled", "start_day": "2", "end_day": "4", "times": "09:00",
                                                "label": "", "from_cycle": "1"}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/slot", data={"plan_id": dex["id"], "slot": f"{y} 09:00", "status": "taken", "taken_at": f"{y}T09:05",
                               "back": "/"})
    with session(cfg.db_path) as conn:
        event = conn.execute("SELECT id FROM history WHERE entity = 'dose' AND action = 'deleted' AND ref = ?",
                             (str(first),)).fetchone()["id"]
    r = client.post(f"/history/{event}/put-back", data={"back": "/history"}, follow_redirects=False)
    assert r.status_code == 409 and "Check before you put this back" in r.text
    assert re.search(r'class="btn btn-primary btn-lg" href="/history">Leave it as it is', r.text)
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs WHERE medication_id = ?", (dex["medication_id"],)).fetchone()[0] == 1
    r = client.post(f"/history/{event}/put-back", data={"back": "/history", "confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/dose/{first}"
