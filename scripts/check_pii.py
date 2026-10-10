#!/usr/bin/env python3
"""Block personal information (and local-only AI assistant files) from entering the repo.

Two layers:

1. Generic checks that live in the repo and run everywhere, including CI:
   files that hold real data (databases, spreadsheets, CSV exports, photos of
   paperwork, .env, data/ and backups/), email addresses, phone numbers that
   aren't 555-01xx placeholders, and SSN-shaped numbers.

2. A private list of real terms (names, the exact label strengths, clinic
   name, ...) in .private/pii-denylist.txt. That file is git-ignored, so it
   only exists on machines that hold the real data. One term per line, case
   insensitive. Lines starting with "re:" are regular expressions. Lines
   starting with "#" are comments.

Matches are reported as file:line plus the rule name only. The matched text
is never printed, so a blocked commit can't leak the term into a terminal,
CI log, or chat transcript.

    python3 scripts/check_pii.py --staged   # what the pre-commit hook runs
    python3 scripts/check_pii.py --all      # every tracked file (CI)
    python3 scripts/check_pii.py --history  # every line ever committed (local, uses the private list)

Standard library only, so the hook works without the project environment.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
DENYLIST = Path(os.environ.get("CHEMO_PII_DENYLIST", ROOT / ".private" / "pii-denylist.txt"))

BLOCKED_DIRS = ("data/", "backups/", ".private/")
BLOCKED_SUFFIXES = (".sqlite3", ".sqlite", ".db", ".xlsx", ".xls", ".csv", ".heic", ".jpg", ".jpeg", ".pdf")
BLOCKED_NAMES = (".env", ".secret_key")
# Images are only allowed where they are known to be safe.
PNG_ALLOWED = ("src/chemotracker/static/", "docs/screenshots/")
SKIP_CONTENT = ("uv.lock",)
# AI assistant config and prompts are kept local, so the public repo stays about the app.
AI_FILES = ("agents.md", "claude.md", "gemini.md", ".cursorrules", ".windsurfrules", "copilot-instructions.md")
AI_DIRS = (".claude/", ".codex/", ".cursor/", ".windsurf/", ".continue/", ".gemini/", ".hermes/",
           ".interface-design/", ".impeccable/", ".aider")
BINARY_SUFFIXES = (".png", ".woff2", ".woff", ".ico", ".gif")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_OK = re.compile(r"@(example\.(com|org|net)|users\.noreply\.github\.com)$", re.I)
PHONE = re.compile(r"(?<![\w.])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w.])")
# Fictional: the reserved 555-0100..0199 lines, and the unassigned 555 area code.
PHONE_OK = re.compile(r"555[\s.-]01\d\d|^\+?1?[\s.-]?\(?555\)?[\s.-]")
SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")


@dataclass
class Finding:
    path: str
    line: int | None
    rule: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"  {where}: {self.rule}"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def load_private_terms(path: Path = DENYLIST) -> list[tuple[str, re.Pattern[str]]]:
    if not path.is_file():
        return []
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        number = len(terms) + 1
        if line.startswith("re:"):
            pattern = re.compile(line[3:].strip(), re.I)
        else:
            pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(line) + r"(?![A-Za-z0-9])", re.I)
        terms.append((f"private term #{number}", pattern))
    return terms


def check_path(path: str) -> list[Finding]:
    p = PurePosixPath(path)
    name = p.name.lower()
    out = []
    if path.startswith(BLOCKED_DIRS):
        out.append(Finding(path, None, "file in a private folder (data/, backups/, .private/)"))
    if name in BLOCKED_NAMES or name.endswith(BLOCKED_SUFFIXES) or ".sqlite3-" in name:
        out.append(Finding(path, None, "file type that holds real data (database, spreadsheet, export, photo, secrets)"))
    if name.endswith(".png") and not path.startswith(PNG_ALLOWED):
        out.append(Finding(path, None, "image outside src/chemotracker/static/ or docs/screenshots/"))
    if name in AI_FILES or any(part.startswith(AI_DIRS) or f"{part}/".startswith(AI_DIRS) for part in p.parts):
        out.append(Finding(path, None, "AI assistant file (keep it local; it's git-ignored)"))
    return out


def check_text(path: str, text: str, private: list[tuple[str, re.Pattern[str]]]) -> list[Finding]:
    out = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in EMAIL.finditer(line):
            if not EMAIL_OK.search(match.group(0)):
                out.append(Finding(path, number, "email address"))
        for match in PHONE.finditer(line):
            if not PHONE_OK.search(match.group(0)):
                out.append(Finding(path, number, "phone number (use 555-0100 to 555-0199 for examples)"))
        if SSN.search(line):
            out.append(Finding(path, number, "SSN-shaped number"))
        for rule, pattern in private:
            if pattern.search(line):
                out.append(Finding(path, number, rule))
    return out


def scan(files: dict[str, str | None], private: list[tuple[str, re.Pattern[str]]]) -> list[Finding]:
    findings: list[Finding] = []
    for path, text in sorted(files.items()):
        findings += check_path(path)
        if text is not None and not path.endswith(SKIP_CONTENT):
            findings += check_text(path, text, private)
    return findings


def _read_blob(spec: str) -> str | None:
    data = subprocess.run(["git", "show", spec], cwd=ROOT, capture_output=True).stdout
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace")


def staged_files() -> dict[str, str | None]:
    names = [n for n in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0") if n]
    return {n: (None if n.endswith(BINARY_SUFFIXES) else _read_blob(f":{n}")) for n in names}


def tracked_files() -> dict[str, str | None]:
    names = [n for n in git("ls-files", "-z").split("\0") if n]
    out: dict[str, str | None] = {}
    for n in names:
        path = ROOT / n
        if n.endswith(BINARY_SUFFIXES) or not path.is_file():
            out[n] = None
            continue
        data = path.read_bytes()
        out[n] = None if b"\0" in data[:8000] else data.decode("utf-8", errors="replace")
    return out


def history_lines() -> dict[str, str | None]:
    """Every added line in every commit, grouped per file path."""
    log = git("log", "--all", "-p", "--no-color", "--format=commit %H%n%B")
    out: dict[str, list[str]] = {"(commit messages)": []}
    current = "(commit messages)"
    for line in log.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            out.setdefault(current, [])
        elif line.startswith("commit "):
            current = "(commit messages)"
        elif line.startswith("+") and not line.startswith("+++"):
            out.setdefault(current, []).append(line[1:])
        elif current == "(commit messages)":
            out[current].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="check what is about to be committed (default)")
    mode.add_argument("--all", action="store_true", help="check every tracked file")
    mode.add_argument("--history", action="store_true", help="check every line in git history")
    args = ap.parse_args(argv)

    private = load_private_terms()
    if args.history:
        files = history_lines()
        findings = [f for path, text in files.items() for f in check_text(path, text or "", private)]
        findings += [f for path in files if not path.startswith("(") for f in check_path(path)]
    else:
        findings = scan(tracked_files() if args.all else staged_files(), private)

    note = f"with {len(private)} private terms" if private else "generic checks only (no .private/pii-denylist.txt)"
    if findings:
        print(f"Personal information check FAILED ({note}). Nothing matched is shown here, on purpose.", file=sys.stderr)
        for f in findings:
            print(f, file=sys.stderr)
        print("Use placeholders (John, Jane, Sam, 555-0100, example.com) or keep the file out of git.", file=sys.stderr)
        return 1
    print(f"Personal information check passed ({note}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
