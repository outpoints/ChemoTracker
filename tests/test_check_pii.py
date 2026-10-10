"""The personal-information checker that guards the repo."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check_pii", ROOT / "scripts" / "check_pii.py")
assert spec and spec.loader
check_pii = importlib.util.module_from_spec(spec)
sys.modules["check_pii"] = check_pii  # dataclasses look the module up by name
spec.loader.exec_module(check_pii)


def rules(text: str, private=()) -> list[str]:
    return [f.rule for f in check_pii.check_text("x.md", text, list(private))]


def test_placeholders_pass():
    assert rules("Call (555) 010-0100 or 555-0199 or (206) 555-0142, write to jane@example.com") == []
    assert rules("commit by 12377381+someone@users.noreply.github.com") == []
    assert rules("Day 2026-10-09 at 21:30, version 3.12.12, 100.4 F") == []


def test_real_contact_details_fail():
    # Samples are assembled at runtime so this file never contains a real-looking value itself.
    dash, at = "-", "@"
    phone = "Call " + dash.join(["206", "555", "1234"])
    spaced = "(" + "312" + ") " + "867" + " " + "5309"
    email = "mail someone" + at + "gmail" + ".com"
    ssn = "SSN " + dash.join(["123", "45", "6789"])
    assert rules(phone) == ["phone number (use 555-0100 to 555-0199 for examples)"]
    assert rules(spaced) == ["phone number (use 555-0100 to 555-0199 for examples)"]
    assert rules(email) == ["email address"]
    assert rules(ssn) == ["SSN-shaped number"]


def test_blocked_paths():
    def blocked(path: str) -> bool:
        return bool(check_pii.check_path(path))

    for path in ["data/chemotracker.sqlite3", "backups/x.xlsx", ".private/DECISIONS.md", ".env", "report.csv",
                 "scan.jpeg", "notes/label.png", "chemotracker.sqlite3-wal", "AGENTS.md", "CLAUDE.md",
                 ".claude/settings.json", ".cursor/rules/x.mdc", ".interface-design/system.md"]:
        assert blocked(path), path
    for path in ["README.md", "CONTRIBUTING.md", "src/chemotracker/static/icon.svg", "docs/screenshots/today.png",
                 "src/chemotracker/web.py", ".env.example", ".github/workflows/ci.yml"]:
        assert not blocked(path), path


def test_private_terms_match_whole_words_and_never_echo(tmp_path, capsys, monkeypatch):
    denylist = tmp_path / "deny.txt"
    denylist.write_text("# comment\nZelda\nre:secretmed\\W+\\d+\\s*mg\n")
    terms = check_pii.load_private_terms(denylist)
    assert [t[0] for t in terms] == ["private term #1", "private term #2"]
    assert rules("Logged by Zelda", terms) == ["private term #1"]
    assert rules("Zeldas and Zeldarian", terms) == []  # whole words only
    assert rules("SecretMed 40 mg", terms) == ["private term #2"]

    finding = check_pii.Finding("notes.md", 3, "private term #1")
    assert "Zelda" not in str(finding)


def test_current_tree_is_clean():
    findings = check_pii.scan(check_pii.tracked_files(), [])
    assert not findings, [str(f) for f in findings]
