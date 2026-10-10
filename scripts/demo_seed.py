"""Fill a throwaway database with made-up example data, for demos and README screenshots.

Every name and dose here is fictional. Point it at an empty folder, never at
your real data folder:

    uv run python scripts/demo_seed.py /tmp/chemotracker-demo
    CHEMO_DATA_DIR=/tmp/chemotracker-demo/data uv run chemotracker
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from chemotracker import profiles, schedule, store
from chemotracker.config import load_settings
from chemotracker.db import migrate, put_settings, session

# Fictional example values for a demo, not dosing guidance.
MEDICINES = {
    "doxorubicin": {"plan": {"kind": "clinic", "days": ["1"], "label": "Chemo infusion"}},
    "cyclophosphamide": {"plan": {"kind": "clinic", "days": ["1"], "label": "Chemo infusion"}},
    "palonosetron": {"plan": {"kind": "clinic", "days": ["1"], "label": "Before chemo"}},
    "dexamethasone": {"strength": "4 mg tablet", "dose": "1 tablet", "directions": "Example: 1 tablet with breakfast",
                      "plan": {"kind": "scheduled", "days": ["2", "3"], "times": ["08:00"], "label": "With food"}},
    "olanzapine": {"strength": "2.5 mg tablet", "dose": "1 tablet", "directions": "Example: 1 tablet at bedtime",
                   "plan": {"kind": "scheduled", "days": ["1", "2", "3", "4"], "times": ["21:30"], "label": "At bedtime"}},
    "pegfilgrastim": {"directions": "Example: on-body injector applied at the clinic",
                      "plan": {"kind": "scheduled", "days": ["2"], "times": ["14:00"], "label": "Check the injector light"}},
    "ondansetron": {"strength": "4 mg tablet", "dose": "1 tablet", "directions": "Example: 1 tablet every 8 hours if needed",
                    "min_hours_between": "8", "plan": {"kind": "as_needed", "days": ["2", "3", "4", "5", "6", "7"],
                                                       "label": "If nauseated"}},
    "prochlorperazine": {"strength": "5 mg tablet", "dose": "1 tablet", "directions": "Example: 1 tablet every 6 hours if needed",
                         "min_hours_between": "6", "plan": {"kind": "as_needed", "days": ["1", "2", "3", "4", "5"],
                                                            "label": "If nauseated"}},
    "senna": {"strength": "8.6 mg tablet", "dose": "2 tablets", "directions": "Example: 2 tablets at bedtime if needed",
              "plan": {"kind": "as_needed", "days": ["1"], "whole_cycle": "1", "label": "For constipation"}},
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, help="empty folder for the demo (data/ and backups/ are created inside)")
    ap.add_argument("--day", type=int, default=3, help="which cycle day 'today' should be (default 3)")
    ap.add_argument("--force", action="store_true", help="replace an existing demo database in that folder")
    args = ap.parse_args()

    cfg = load_settings(args.folder / "data", args.folder / "backups")
    if cfg.db_path.exists():
        if not args.force:
            sys.exit(f"{cfg.db_path} already exists. Use --force to replace this demo database.")
        for suffix in ("", "-wal", "-shm"):
            Path(f"{cfg.db_path}{suffix}").unlink(missing_ok=True)
    migrate(cfg.db_path)
    now = datetime.now(cfg.tz).replace(tzinfo=None, second=0, microsecond=0)
    today = now.date()

    with session(cfg.db_path) as conn:
        john = profiles.create(conn, "John", "patient")
        jane = profiles.create(conn, "Jane", "family")
        sam = profiles.create(conn, "Sam", "caregiver")
        for key, values in MEDICINES.items():
            values = dict(values)
            plan = values.pop("plan")
            med_id = store.add_from_catalog(conn, key)
            store.update_medication(conn, med_id, values)
            store.update_plan(conn, store.plans(conn, med_id)[0]["id"], plan)
        store.create_cycle(conn, {"number": "1", "day1": (today - timedelta(days=args.day - 1)).isoformat(),
                                  "length_days": "14", "regimen": "Example: AC"})
        put_settings(conn, {"clinic_name": "Example Cancer Center", "clinic_phone": "(555) 010-0100",
                            "after_hours_phone": "(555) 010-0199", "fever_f": "100.4", "onboarded": "1"})

        cycle = schedule.cycle_for_date(conn, today)
        assert cycle is not None
        who = [john, jane, sam]
        for i, s in enumerate(schedule.slots(conn, cycle)):
            if s.day < 1:
                continue
            if s.date < today or (s.time and s.date == today and s.dt <= now - timedelta(hours=1)):
                taken = s.dt + timedelta(minutes=7 + i % 9) if s.time else datetime.combine(s.date, datetime.min.time()).replace(hour=10)
                log = store.log_slot(conn, s.plan, s.key, "taken", taken, s.dose or None, by=who[i % 3])
                if s.kind != "clinic":
                    store.quick_follow_up(conn, log, "yes", "right", by=john)

        comp = conn.execute("SELECT id FROM medications WHERE info_key = 'prochlorperazine'").fetchone()["id"]
        prn = store.log_dose(conn, comp, now - timedelta(hours=2, minutes=20), "1 tablet", "Nausea", by=john)
        store.update_dose_log(conn, prn, {"status": "taken", "taken_at": (now - timedelta(hours=2, minutes=20)).strftime("%Y-%m-%d %H:%M"),
                                          "dose": "1 tablet", "reason": "Nausea", "helped": "some", "dose_feel": "not_enough",
                                          "side_effects[]": ["Drowsy"]}, now, by=jane)

        yesterday = now - timedelta(days=1)
        entries = [
            (yesterday.replace(hour=9, minute=40), {"symptom": "nausea", "severity": "2", "notes": "After breakfast"}, john),
            (yesterday.replace(hour=14, minute=5), {"symptom": "fatigue", "severity": "2"}, jane),
            (yesterday.replace(hour=20, minute=15), {"symptom": "temperature", "value": "99.0"}, sam),
            (now.replace(hour=7, minute=50), {"symptom": "nausea", "severity": "1"}, john),
            (now.replace(hour=11, minute=30), {"symptom": "fatigue", "severity": "2", "notes": "Napped after lunch"}, john),
            (now - timedelta(hours=2, minutes=30), {"symptom": "nausea", "severity": "2", "notes": "Took Compazine"}, john),
            (now - timedelta(hours=1), {"symptom": "temperature", "value": "98.9"}, jane),
        ]
        for when, values, by in entries:
            if when <= now:
                store.add_symptom(conn, {**values, "logged_at": when.strftime("%Y-%m-%d %H:%M")}, now, by=by)
    print(f"Demo database ready: {cfg.db_path}")
    print(f"Run: CHEMO_DATA_DIR={cfg.data_dir} CHEMO_BACKUP_DIR={cfg.backup_dir} uv run chemotracker")


if __name__ == "__main__":
    main()
