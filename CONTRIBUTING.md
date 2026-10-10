# Contributing

Thank you for helping. ChemoTracker started as one family's tool for getting through chemotherapy, and the goal is to make it just as easy for any family going through cancer. Pull requests are welcome, small or large. You don't need to be an expert, and you don't need permission to open one.

## Ways to help

You don't have to write code to help.

- **Tell us what was confusing.** If a screen made you or someone in your family stop and wonder what to do, open an issue and describe it. These reports are the most useful thing you can send.
- **Prescription wording the app didn't understand.** Paste the *directions* only, with every name, number and date changed (see Rule 1), and say what you expected. These become new test cases in `tests/test_rxparse.py`.
- **Medicines that are missing** from the built-in list (`src/chemotracker/medinfo.py`). Add a plain-language summary and link its MedlinePlus or national formulary page as the source.
- **How treatment works where you live.** Different countries and clinics write schedules, units and labels differently. Examples help the app work for more people.
- **Translations, accessibility, and testing on older phones.** The people using this are often tired, unwell, or not comfortable with technology. Bigger text, clearer words, and fewer taps all count.
- **Code.** Bug fixes, features, and tests.

## Before you start

1. Read Rule 1 below. It matters more than anything else here.
2. For anything bigger than a small fix, open an issue first so we can agree on the approach.
3. Keep each pull request to one change. Smaller ones are easier to review and safer to roll back.

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
- `.private/`: personal notes, design work, and the private term list described below

### How it's enforced

- `scripts/check_pii.py` blocks database, spreadsheet, export and photo files, `.env`, anything under the private folders, email addresses, non-555 phone numbers, SSN-shaped numbers, and local-only assistant files.
- **Private term list**: on machines that hold real data, `.private/pii-denylist.txt` lists the real names and label details to block. It is git-ignored, so the real terms never reach GitHub. When something matches, the checker prints only the file, the line, and "private term #N", never the text itself.
- **Pre-commit hook**: turn it on once per clone with `git config core.hooksPath .githooks`.
- **CI** runs the generic checks and the tests on every pull request. CI never sees the private list.

To scan everything already committed: `python3 scripts/check_pii.py --history`.

If personal information is ever committed, don't just delete it in a new commit, because it stays in history. Stop, tell the maintainer, rewrite history (for example with `git filter-repo`), force-push, and treat the data as exposed.

## Rule 2: design for the least technical person in the family

Assume the person holding the phone is tired, worried, and has never used an app like this.

- Use plain words. Say "Day 1 is chemo day", not "C1D1". Explain abbreviations the first time they appear.
- One obvious next step per screen, with big tap targets.
- Never lose what someone typed. Show a clear message and keep their input.
- Ask before doing something surprising, like logging a dose earlier than the label allows or stopping a medicine.
- Every guess the app makes, like default times or days, is shown and easy to change.
- The app keeps a record. It never tells someone how to take a medicine. The label and the care team come first.

## Rule 3: keep the repo about the app

Editor and assistant config (for example `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.cursor/`), prompt files, and design scratch work stay on your machine. They're git-ignored, and the checker refuses them too.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
git config core.hooksPath .githooks       # the personal-information check runs before every commit
uv sync
uv run pytest
uv run python scripts/demo_seed.py /tmp/chemotracker-demo
CHEMO_DATA_DIR=/tmp/chemotracker-demo/data CHEMO_BACKUP_ENABLED=0 uv run chemotracker
```

Before you open a pull request:

```bash
uv run pytest
python3 scripts/check_pii.py --staged
git diff --cached --check
```

- Add or update tests for what you changed. The tests use only placeholder data. Keep it that way.
- Screenshots for the README come from `scripts/demo_seed.py` data only, using `scripts/screenshots.py`.
- If you change how prescriptions are read, update `docs/prescription-formats.md` and cite your source.
- Database changes go in a new entry at the end of `MIGRATIONS` in `src/chemotracker/db.py`. Never edit an old one, because existing installs have already run it.

## Medical content

Medicine summaries are written for families, not clinicians, and every summary links its source. Don't add dosing advice. If you're unsure whether something belongs, open an issue and ask.
