# ChemoTracker

I built ChemoTracker for my mom when she started chemotherapy. The clinic sent us home with a paper sheet of anti-nausea medicines, pharmacy bottles on different schedules, and a list of symptoms to watch for. Several of us help with her medicines, and keeping track of who gave what and when, then showing her care team what actually happened, was harder than it should have been.

ChemoTracker is one shared record of the medicines and symptoms around each chemo cycle. It runs on a computer at home, and everyone opens it in the browser on their own phone. It's built for the person who is worn out and nauseous at 2 AM, the family members taking turns, and the care team reading the report afterwards.

I'd like it to work as well for other families going through cancer as it does for mine. Pull requests are welcome, from a confusing word to a missing medicine to a whole feature. See [CONTRIBUTING.md](CONTRIBUTING.md).

> [!WARNING]
> **ChemoTracker is almost entirely vibe-coded.** I built it with AI coding tools and shaped it around what my mom and the rest of the family needed as we used it. I've hardened it as well as I can, but it has never had a professional security review.
>
> **Run it on your home network or a private network like Tailscale, and never expose it to the open internet.** [SECURITY.md](SECURITY.md) explains how to run it safely.

<p>
  <img src="docs/screenshots/today-phone.png" alt="Today on a phone: cycle ring, symptom buttons, time-of-day compartments" width="260">
  <img src="docs/screenshots/symptom-saved-phone.png" alt="After logging nausea: your as-needed medicines and when each is allowed" width="260">
  <img src="docs/screenshots/today-phone-dark.png" alt="Today on a phone in the dark theme" width="260">
</p>
<img src="docs/screenshots/today-desktop.png" alt="Today on a computer" width="800">

Screenshots use made-up demo data.

## What it does

- **Today** shows the day's doses in Morning, Afternoon, Evening and Bedtime compartments, like a pill organizer, plus clinic medicines on chemo day. Tap **Taken**, say when it was taken, and it's saved with your name. A wrong tap can be undone, and if someone else already logged that dose, the app says so instead of saving it twice.
- **As-needed medicines** show a ring that counts down to when the label allows the next dose. Logging one sooner than the label allows asks first.
- **Symptoms** covers nausea, vomiting, temperature, pain, tiredness and more, answered in plain words or a number. After nausea, the app lists your nausea medicines and when each one is OK next. A fever or anything severe puts the care team's phone number at the top.
- **The cycle** is a ring with a dot for each day, plus a day-by-day sheet like the clinic's paper handout. Day 1 is chemo day. Plans can repeat every 1 to 4 weeks and have a second part ("AC every 14 days x 4, then paclitaxel weekly x 12"). When chemo is delayed, move Day 1 and the doses move with it.
- **Medicines** go in by pasting what's printed on the bottle ("dexAMETHasone 4MG TAB - Take 2 tablets by mouth daily. Take on days 2, 3, AND 4 AFTER CHEMO THEN STOP") and the app fills in the dose, how often and which days. It shows what it understood and marks every guess before saving ([what it reads](docs/prescription-formats.md)). A built-in list of 50 medicines has plain-language summaries from MedlinePlus.
- **Report** makes an Excel workbook and CSV files for the care team: every dose, symptom and later edit, and who logged it. Planned doses that were never logged show as "Not logged", so gaps are visible.
- **Reminders** reach each phone through the free [ntfy](https://ntfy.sh) app: when a dose is due, when a shot needs to come out of the fridge, when a dose still isn't marked, and the evening before chemo.
- **Change history** records every edit and who made it, and deleted entries can be put back. The app also backs itself up every night.

It works in light or dark (following the phone), in °C or °F, and uses Atkinson Hyperlegible, a typeface made for low vision.

ChemoTracker keeps a record. It doesn't give medical advice. The pharmacy label and your care team's instructions always come first.

## Run it

You need Docker on a computer that stays on at home. The [setup guide](docs/setup.md) goes step by step, from installing Docker to showing the family how to use it, and covers reminders, a NAS and using it away from home.

```bash
git clone https://github.com/outpoints/ChemoTracker.git
cd ChemoTracker
cp .env.example .env        # optional: time zone, family password, backup folder
mkdir -p data backups
docker compose up -d --build
```

Open `http://<this-computer>:8000` on any phone or computer on the same network, and add it to each phone's home screen so it opens like an app. The first visit walks through setup: who's using it, chemo day, the medicines, and the care team's phone numbers.

On Linux, the container runs as user 10001 and needs to write to `data` and `backups`. Set `CHEMO_UID` and `CHEMO_GID` in `.env` to your own (`id -u`, `id -g`), or run `sudo chown -R 10001:10001 data backups`.

To update, run `git pull` and then `docker compose up -d --build`. Your data and backups stay where they are, and the app saves a copy before any update that changes the database. To skip the build, each release is also published as an image: set `CHEMO_IMAGE` in `.env` ([setup guide](docs/setup.md#use-a-published-image-instead-of-building)).

## Privacy and security

Everything stays on the computer running ChemoTracker, in the data folder and the backup folder. Nothing is sent anywhere else unless you turn on reminders.

- A family password is optional. Set `CHEMO_PASSWORD` if people outside your household can reach your network. The people you add in the app are names for "logged by", not accounts, so the password is the only lock.
- To use it away from home, set `CHEMO_BIND=127.0.0.1` and `CHEMO_SECURE_COOKIE=1`, then run `tailscale serve --bg 8000`. Only your own devices can reach it, over HTTPS.
- Reminders go through the public ntfy.sh server unless you run your own. Anyone who knows the topic name can read them, so keep it private, or turn off **Show medicine names** in Settings.
- The app uses seven Python packages, each pinned to an exact version with a checksum in `uv.lock`. Fonts and icons are bundled, so pages load nothing from other sites. Dependabot proposes updates weekly, and CI checks for known vulnerabilities.

[SECURITY.md](SECURITY.md) covers reverse proxies, allowed host names, and what is and isn't protected.

## Settings

Set these in `.env`. All of them are optional.

| Variable | Default | What it does |
|---|---|---|
| `TZ` | `America/Los_Angeles` | Time zone for dose times and backups |
| `CHEMO_PASSWORD` | unset | Family password for every page. Leave it out to skip the sign-in screen |
| `CHEMO_PUBLISH_PORT` | `8000` | Port on the host |
| `CHEMO_BIND` | `0.0.0.0` | `0.0.0.0` reaches your home network. `127.0.0.1` keeps it on this computer (for Tailscale serve or a local proxy) |
| `CHEMO_ALLOWED_HOSTS` | unset (any name) | Only answer these names and addresses, comma-separated, e.g. `chemo.example.com,192.168.1.20`. Blocks DNS rebinding |
| `CHEMO_SECURE_COOKIE` | `0` | Set to `1` when the app is only reached over HTTPS |
| `CHEMO_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Your reverse proxy's address, so the app sees each visitor's real address |
| `CHEMO_DATA_HOST_DIR` | `./data` | Host folder for the database, for example on a NAS. It must be on a disk the Docker computer owns, not a network share ([why](docs/setup.md#on-a-nas)) |
| `CHEMO_BACKUP_HOST_DIR` | `./backups` | Host folder for backups. An external drive or a synced folder works well |
| `CHEMO_BACKUP_TIME` | `03:00` | Time of day the backup schedule starts from |
| `CHEMO_BACKUP_INTERVAL_HOURS` | `24` | How often to back up |
| `CHEMO_BACKUP_KEEP` | `30` | How many backups to keep. Older ones are deleted |
| `CHEMO_BACKUP_ENABLED` | `1` | Set to `0` to turn automatic backups off |
| `CHEMO_UID` / `CHEMO_GID` | `10001` | User the container runs as |
| `CHEMO_SECRET_KEY` | generated | Session signing key, stored in `data/.secret_key` if unset |

## Backups and restore

The app backs itself up at 3:00 AM every day. If the computer was off then, it catches up a minute after starting. Each backup is a copy of the database plus the Excel report, which opens without the app.

**Settings → Backups** shows the last and next backup and has **Back up now** and **Download a copy**. Under **Go back to an earlier copy**, pick a backup to see what's in it before restoring. The app saves the current record first, so a restore can be undone.

Don't open `data/chemotracker.sqlite3` in other programs while the app is running, especially from outside Docker. Use **Download a copy** instead.

To restore by hand, for example if the app won't start, run this in the folder with `compose.yaml` (with your own folders if you changed `CHEMO_DATA_HOST_DIR` or `CHEMO_BACKUP_HOST_DIR`):

```bash
docker compose down
cp backups/chemotracker-YYYYMMDD-HHMMSS.sqlite3 data/chemotracker.sqlite3
rm -f data/chemotracker.sqlite3-wal data/chemotracker.sqlite3-shm
docker compose up -d
```

## Development

Requires [uv](https://docs.astral.sh/uv/). Read [CONTRIBUTING.md](CONTRIBUTING.md) first: no personal information ever goes into this repo, and every screen is designed for the least technical person in the family.

```bash
git config core.hooksPath .githooks       # blocks commits that contain personal information
uv sync
uv run pytest
uv run python scripts/demo_seed.py /tmp/chemotracker-demo
CHEMO_DATA_DIR=/tmp/chemotracker-demo/data CHEMO_BACKUP_DIR=/tmp/chemotracker-demo/backups uv run chemotracker
```

That opens a demo at `http://127.0.0.1:8000` with three made-up people (John, Jane and Sam) and example doses. Screenshots for the repo come only from the demo: `uv run --with playwright python scripts/screenshots.py http://127.0.0.1:8000 docs/screenshots`. The setup guide's first-run shots use `--walkthrough` against a second, empty instance.

The app is FastAPI with server-rendered Jinja pages and plain CSS. There's no JavaScript build step, and every page works without JavaScript. SQLite holds the record, because two phones saving to a CSV at the same time would overwrite each other. The Excel and CSV files are generated from it. Planned doses aren't stored. They're computed from each cycle's Day 1 and the medicine's schedule, so moving chemo day moves every dose.

In `src/chemotracker/`:

- `web.py`: routes
- `schedule.py`: cycle days, dose slots, as-needed timing
- `store.py`: reads, writes and validation
- `rxparse.py`: reads typed or pasted prescriptions
- `medinfo.py`: the medicine list
- `symptoms.py`: symptoms and how severe each one is
- `export.py`: Excel and CSV reports
- `reminders.py`: ntfy reminders
- `backup.py`: backups and restore
- `history.py`: change history, written by SQLite triggers in `db.py`
- `profiles.py`: people and who logged what
- `security.py`: cross-site and host checks, security headers, sign-in throttle
- `units.py`: °C and °F
- `templates/` and `static/`: the pages

## To do

ChemoTracker was built around my mom's treatment, so some things aren't done yet.

- [ ] **More than one patient per install.** Today each install tracks one person. A household with two people in treatment needs two installs, each with its own `data` folder and port.
- [ ] **Offline logging.** Phones need to reach the server to log. A safe version would show the last loaded Today page offline, queue taps with a unique ID so they're never saved twice, and on reconnecting show anything that clashed with what someone else logged meanwhile.
- [ ] **Test on a Raspberry Pi.** The app is built to be light (no build step, SQLite, about 4 ms to read a prescription), but it hasn't been run on a Pi yet.

## Credits

- Medicine summaries are adapted from [MedlinePlus](https://medlineplus.gov/druginformation.html) (U.S. National Library of Medicine). Each medicine links to its source page.
- Fonts: [Atkinson Hyperlegible Next and Mono](https://www.brailleinstitute.org/freefont/) (SIL Open Font License, `src/chemotracker/static/fonts/OFL-LICENSE.txt`).
- Icons are adapted from [Lucide](https://lucide.dev) (ISC, `THIRD_PARTY_NOTICES/lucide-icons.txt`).

License: GPL-3.0.
