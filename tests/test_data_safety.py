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

T0 = "2026-01-01T00:00:00+00:00"

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
