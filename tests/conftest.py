from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from chemotracker.config import load_settings
from chemotracker.db import migrate, session
from chemotracker.web import create_app


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("CHEMO_TIMEZONE", "America/Los_Angeles")
    monkeypatch.delenv("CHEMO_PASSWORD", raising=False)
    settings = load_settings(tmp_path / "data", tmp_path / "backups")
    migrate(settings.db_path)
    return settings


@pytest.fixture()
def client(cfg):
    app = create_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def conn(cfg):
    with session(cfg.db_path) as c:
        yield c


def local_now(cfg) -> datetime:
    return datetime.now(cfg.tz).replace(tzinfo=None, second=0, microsecond=0)


def today(cfg) -> date:
    return local_now(cfg).date()


def setup_walkthrough(client, day1: date) -> None:
    """Drive the real first-run walkthrough with the label details from the family's prescriptions."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    keys = ["doxorubicin", "cyclophosphamide", "paclitaxel", "ondansetron", "prochlorperazine", "dexamethasone",
            "olanzapine", "filgrastim"]
    r = client.post("/setup/medicines", data={"keys[]": keys}, follow_redirects=False)
    assert r.headers["location"] == "/setup/label/0"
    labels = {
        # Generic example values, not anyone's real prescriptions.
        "Ondansetron": {"strength": "4 mg tablet", "dose": "1 tablet", "directions": "Example: 1 tablet every 8 hours as needed",
                        "min_hours_between": "8", "kind": "as_needed", "days[]": ["4", "5", "6"]},
        "Prochlorperazine": {"strength": "5 mg tablet", "dose": "1 tablet", "directions": "Example: 1 tablet every 6 hours as needed",
                             "min_hours_between": "6", "kind": "as_needed", "days[]": [str(d) for d in range(1, 7)]},
        "Dexamethasone": {"strength": "2 mg tablet", "dose": "2 tablets (4 mg)",
                          "directions": "Example: 2 tablets once a day with food",
                          "kind": "scheduled", "days[]": ["2", "3", "4"], "times[]": ["08:00"]},
        "Olanzapine": {"strength": "2.5 mg tablet", "dose": "1 tablet", "directions": "Example: 1 tablet at bedtime",
                       "kind": "scheduled", "days[]": ["2", "3", "4"], "times[]": ["21:00"], "label": "At bedtime"},
        "Filgrastim": {"kind": "scheduled", "days[]": [str(d) for d in range(2, 8)], "times[]": ["15:00"]},
    }
    index = 0
    while True:
        page = client.get(f"/setup/label/{index}", follow_redirects=False)
        if page.status_code == 303:
            assert page.headers["location"] == "/setup/cycle"
            break
        assert page.status_code == 200
        name = next(n for n in ["Ondansetron", "Prochlorperazine", "Dexamethasone", "Olanzapine", "Filgrastim",
                                "Doxorubicin", "Cyclophosphamide", "Paclitaxel"] if f"<h1 style=\"margin-top: var(--s1)\">{n}" in page.text)
        data = {"name": name, "route": "oral", **labels.get(name, {"kind": "clinic", "days[]": ["1"]})}
        r = client.post(f"/setup/label/{index}", data=data, follow_redirects=False)
        assert r.status_code == 303, r.text
        index += 1
    r = client.post("/setup/cycle", data={"number": "1", "day1": day1.isoformat(), "length_days": "14", "regimen": "Example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/setup/team"
    r = client.post("/setup/team", data={"clinic_name": "Infusion Center", "clinic_phone": "555-0100",
                                          "after_hours_phone": "555-0199", "fever_f": "100.4", "call_if": "Fever"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"
