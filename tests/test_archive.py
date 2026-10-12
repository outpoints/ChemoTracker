"""Archive: a medicine with logged doses can't be deleted, because the care team's report needs its doses. Archiving
puts it away instead: off Today, reminders, Log a dose and the medicine lists, with its record kept everywhere."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta

from chemotracker import export, schedule, store
from chemotracker.db import MIGRATIONS, migrate, session

from .conftest import local_now, setup_walkthrough, today


def _med(cfg, name):
    with session(cfg.db_path) as conn:
        return conn.execute("SELECT * FROM medications WHERE name = ?", (name,)).fetchone()


def _archived_part(html: str) -> str:
    """The closed Archived fold-out at the bottom of the Medicines page ('' when there is none)."""
    m = re.search(r'<details[^>]*id="archived".*?</details>', html, re.S)
    return m.group(0) if m else ""


def _setup_with_a_dose(client, cfg, name="Prochlorperazine"):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    med = _med(cfg, name)
    client.post("/dose/quick", data={"medication_id": med["id"], "back": "/"})
    return med


def test_a_medicine_offers_delete_until_a_dose_is_logged_then_archive(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    med = _med(cfg, "Prochlorperazine")
    page = client.get(f"/medicines/{med['id']}").text
    assert "Delete this medicine" in page and "Archive this medicine" not in page
    client.post("/dose/quick", data={"medication_id": med["id"], "back": "/"})
    page = client.get(f"/medicines/{med['id']}").text
    assert "Archive this medicine" in page and "Delete this medicine" not in page
    # A page from before still posts delete: it is refused and points to Archive.
    r = client.post(f"/medicines/{med['id']}/delete", follow_redirects=True)
    assert "be deleted" in r.text and "Archive this medicine" in r.text
    assert _med(cfg, "Prochlorperazine") is not None


def test_archiving_takes_it_off_today_lists_and_log_a_dose_but_keeps_the_record(client, cfg):
    med = _setup_with_a_dose(client, cfg)
    ond = _med(cfg, "Olanzapine")  # scheduled at bedtime on Days 2-4: today is Day 2
    with session(cfg.db_path) as conn:
        assert any(s.plan["medication_id"] == ond["id"] for s in schedule.slots_on(conn, today(cfg)))
    for m in (med, ond):
        r = client.post(f"/medicines/{m['id']}/archive", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/medicines")
    for name in ("Prochlorperazine", "Olanzapine"):
        row = _med(cfg, name)
        assert row["active"] == 0 and row["archived_at"], f"{name} should be archived (and so stopped)"

    page = client.get("/medicines").text
    archived = _archived_part(page)
    assert archived and " open" not in archived.split(">", 1)[0], "the Archived fold-out starts closed"
    main = page.replace(archived, "")
    for name, m in (("Prochlorperazine", med), ("Olanzapine", ond)):
        assert f"/medicines/{m['id']}\"" in archived and name in archived
        assert f"/medicines/{m['id']}\"" not in main, f"{name} should only be under Archived"
    assert 'id="archived"' in page and "Stopped</h2>" not in page, "archived medicines aren't listed as stopped"

    new = client.get("/dose/new").text
    assert f'value="{med["id"]}"' not in new and f'value="{ond["id"]}"' not in new
    with session(cfg.db_path) as conn:
        assert not [s for s in schedule.slots_on(conn, today(cfg)) if s.plan["medication_id"] == ond["id"]]
        rows = export.dose_rows(conn, today(cfg) - timedelta(days=2), today(cfg), local_now(cfg))
    assert any(r.medicine.startswith("Prochlorperazine") and r.status == "Taken" for r in rows), \
        "an archived medicine's doses stay in the care team's report"
    assert "Prochlorperazine" in client.get("/history").text

    # Its own page still opens, says it's archived, and offers to start it again (no delete, no second archive).
    page = client.get(f"/medicines/{med['id']}").text
    assert "archived" in page and "Start again" in page
    assert "Archive this medicine" not in page and "Delete this medicine" not in page


def test_start_again_brings_an_archived_medicine_back(client, cfg):
    med = _setup_with_a_dose(client, cfg)
    client.post(f"/medicines/{med['id']}/archive")
    client.post(f"/medicines/{med['id']}/active", data={"active": "1"})
    row = _med(cfg, "Prochlorperazine")
    assert row["active"] == 1 and row["archived_at"] is None
    page = client.get("/medicines").text
    assert f"/medicines/{med['id']}\"" in page.replace(_archived_part(page), "")


def test_adding_an_archived_catalog_medicine_again_brings_back_the_same_one(client, cfg):
    med = _setup_with_a_dose(client, cfg)
    client.post(f"/medicines/{med['id']}/archive")
    with session(cfg.db_path, write=True) as conn:
        again = store.add_from_catalog(conn, med["info_key"])
    assert again == med["id"], "re-adding must not create a second Prochlorperazine with its own gap and limit"
    row = _med(cfg, "Prochlorperazine")
    assert row["active"] == 1 and row["archived_at"] is None


def test_stopping_and_archiving_are_in_change_history(client, cfg):
    med = _setup_with_a_dose(client, cfg)
    client.post(f"/medicines/{med['id']}/archive")
    with session(cfg.db_path) as conn:
        events = conn.execute("SELECT * FROM history WHERE entity = 'medication' AND ref = ? AND action = 'changed'",
                              (str(med["id"]),)).fetchall()
    assert events
    before, after = json.loads(events[-1]["before"]), json.loads(events[-1]["after"])
    assert before["archived_at"] is None and after["archived_at"] and after["active"] == 0
    page = client.get(f"/medicines/{med['id']}").text
    assert "Archived" in page


def test_an_archived_medicine_keeps_counting_for_the_double_dose_checks(client, cfg):
    """Archiving Prochlorperazine and starting it again must not lose the dose just taken."""
    med = _setup_with_a_dose(client, cfg)
    client.post(f"/medicines/{med['id']}/archive")
    client.post(f"/medicines/{med['id']}/active", data={"active": "1"})
    with session(cfg.db_path) as conn:
        m = store.medication(conn, med["id"])
        assert schedule.dose_warning(conn, m, local_now(cfg), now=local_now(cfg)), \
            "a dose taken minutes ago still makes another one too soon"


def test_upgrade_adds_archived_at_without_touching_existing_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.close()
    migrate(path)
    conn = sqlite3.connect(path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(medications)")]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == len(MIGRATIONS)
    assert cols[-1] == "archived_at" and cols.count("archived_at") == 1, "added at the end, so older code reads it fine"
