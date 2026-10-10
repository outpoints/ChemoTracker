# Contributing

## Rule 1: no personal information in the repo

This app stores someone's cancer treatment. Nothing that identifies a real person, or their real treatment, goes into git: not in code, tests, fixtures, screenshots, docs, commit messages, issue text, or branch names.

That includes:

- Names of the patient, family, caregivers, doctors, nurses, clinics, pharmacies
- Phone numbers, emails, addresses, dates of birth, MRNs or patient IDs, insurance details
- Real prescriptions: exact strengths, doses, label directions, schedules, appointment dates
- Photos or scans of clinic paperwork, pharmacy labels, or test results
- Database files, backups, Excel or CSV exports, `.env`

Use placeholders instead:

| Instead of | Use |
|---|---|
| Real people | John (patient), Jane (family), Sam (caregiver) |
| Phone numbers | 555-0100 to 555-0199 |
| Emails, websites | `@example.com` |
| Clinic | Example Cancer Center |
| Doses and schedules | Values different from any real prescription, labelled "Example:" |

Real data lives only in these git-ignored folders on the machine that runs the app:

- `data/`: the database
- `backups/`: automatic backups
- `.private/`: decision notes, design work, and the private term list described below

### How it's enforced

- `scripts/check_pii.py` blocks database, spreadsheet, export and photo files, `.env`, anything under the private folders, email addresses, non-555 phone numbers, SSN-shaped numbers, and local-only assistant files.
- **Private term list**: on machines that hold real data, `.private/pii-denylist.txt` lists the real names and label details to block. It is git-ignored, so the real terms never reach GitHub. When something matches, the checker prints only the file, the line, and "private term #N", never the text itself.
- **Pre-commit hook**: turn it on once per clone with `git config core.hooksPath .githooks`.
- **CI** runs the generic checks and the tests on every push. CI never sees the private list.

To scan everything already committed: `python3 scripts/check_pii.py --history`.

If personal information is ever committed, don't just delete it in a new commit, because it stays in history. Stop, tell the repo owner, rewrite history (for example with `git filter-repo`), force-push, and treat the data as exposed.

## Rule 2: keep the repo about the app

The repo is meant to be public. Editor and assistant config (for example `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.cursor/`), prompt files, and design scratch work stay on your machine. They're git-ignored, and the checker refuses them too.

## Rule 3: commit and push each major feature

Each major feature gets its own commit, pushed and tagged (`v0.3.0` and so on), so there is always a safe point to roll back to. Before each commit:

```bash
uv run pytest
python3 scripts/check_pii.py --staged     # the hook runs this too
git diff --cached --check
```

## Development

See the README for setup. The tests use only placeholder data. Keep it that way.
