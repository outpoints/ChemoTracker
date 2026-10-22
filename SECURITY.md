# Security

## Please read this first

ChemoTracker is almost entirely vibe-coded. I built it on the fly with AI coding tools, shaped by feedback from my mom and other family members while we used it during her treatment. I've done my best to harden it, and everything below is tested, but it has never had a professional security review.

So treat it like any small home project that holds health information:

- **Run it on your home network (LAN) or a private network like [Tailscale](https://tailscale.com).** Those are the ways I use it and the ways it's built for.
- **Never expose it to the open internet.** Don't forward its port on your router, and don't put it behind a public reverse proxy or tunnel. I don't expose any of my own services to the internet, and this shouldn't be the first.
- It keeps a record. It doesn't give medical advice, and it isn't a medical device.

## Ways to reach it

Pick whichever fits your home. None of them needs the internet to be open.

| Setup | How | Notes |
|---|---|---|
| Home network | Default. Open `http://<computer's address>:8000` | Anyone on your Wi-Fi can open it. Put guests on a guest network, or set a password |
| A name on your home DNS | Point a name like `chemo.example.com` at the computer in Pi-hole or your router | Works with no setting. Optionally add the name to `CHEMO_ALLOWED_HOSTS` (below) |
| Tailscale | Set `CHEMO_BIND=127.0.0.1`, then run `tailscale serve --bg 8000` | Only your Tailscale devices can reach it, over HTTPS, from anywhere. Set `CHEMO_SECURE_COOKIE=1` |
| Reverse proxy in your homelab | Proxy to the container from Caddy, Traefik, nginx, etc. on your LAN | Keep the proxy local-only. See "Behind a reverse proxy" |

### Optional settings

Set these in `.env`. All of them are off by default, so nothing breaks until you choose them.

- `CHEMO_PASSWORD`: one family password for every page. Worth setting if anyone outside the household can reach your network. Five wrong tries from one device means a 15 minute wait.
- `CHEMO_ALLOWED_HOSTS`: a comma-separated list of names and addresses the app answers to, for example `chemo.example.com,192.168.1.20`. Anything else gets an "Unknown address" page. This blocks "DNS rebinding", a trick where a malicious website points its own name at your home network to read the app through your browser. It's the same idea as Django's `ALLOWED_HOSTS`. Leave it unset if you reach the app by many different names. `localhost` and `127.0.0.1` always work so Docker's health check keeps running.
- `CHEMO_SECURE_COOKIE=1`: when the app is only reached over HTTPS (Tailscale serve or a reverse proxy with a certificate). The browser then never sends the session cookie over plain HTTP.
- `CHEMO_BIND=127.0.0.1`: only the computer running it can connect (for Tailscale serve or a proxy on the same machine).

### Behind a reverse proxy

- Pass the original `Host` header (most proxies do), or `X-Forwarded-Host`.
- Set `CHEMO_FORWARDED_ALLOW_IPS` to the proxy's address (or `*` if only the proxy can reach the container's port). Otherwise every visitor looks like the proxy, and the password lock-out would lock out everyone at once.
- Don't add authentication bypasses or public routes for it.

## What's built in

- **No changes from other websites.** Every save must come from ChemoTracker's own pages (the browser's `Origin` or `Referer` must match). A page elsewhere on the internet can't make a family member's browser log or delete anything, even with no password set.
- **Strict browser rules.** A Content Security Policy allows only the app's own scripts, styles, fonts and images. There are no inline scripts and nothing loads from other sites. Pages can't be put in frames, and links to other sites don't send the page address.
- **No outside resources.** Fonts and icons are bundled. The only outgoing connection is the optional reminder service (ntfy), and only if you turn it on.
- **A locked-down container.** It runs as an unprivileged user, with a read-only system, every Linux capability dropped, and `no-new-privileges`.
- **Safe with your data.** Database writes are all-or-nothing, backups are checked before they're kept, and the database is copied before every upgrade. Every change is in the change history with who made it. See the README for details.
- **The session cookie** is signed, unreadable by scripts, and not sent on cross-site requests (`SameSite=Lax`). The signing key is generated per install and stored with owner-only permissions.
- **Restoring a backup** only accepts files from the backup folder by name, or an upload that's checked to be a real ChemoTracker database first.

## Dependencies

To run it you only need Docker. The app uses seven Python packages: FastAPI, Uvicorn, Jinja2, python-multipart, openpyxl, itsdangerous and tzdata, plus the 14 packages they bring in. There are no JavaScript packages, and the browser code is two small files in `src/chemotracker/static/js/`.

Everything is pinned so a build can't quietly pull in something new:

- `pyproject.toml` lists exact versions.
- `uv.lock` pins every package, including indirect ones, with a SHA-256 checksum. The Docker build installs only from this lock (`uv sync --frozen`) and refuses to continue if anything doesn't match.
- The Docker base images (Python and uv) are pinned by digest.
- The app's own code is copied in as plain source, so no packaging tool is downloaded at build time.
- The GitHub Actions used for CI are pinned to exact commits, not tags.
- Published images (`ghcr.io/outpoints/chemotracker`) are built by GitHub Actions from the tagged commit, started once as a check before they're pushed, and carry an SBOM and signed build provenance. Check one with `gh attestation verify oci://ghcr.io/outpoints/chemotracker:<version> --repo outpoints/ChemoTracker`.

Pinning means updates don't arrive by themselves, so:

- **Dependabot** opens a pull request each week when a pinned package, base image or action has an update. Each one runs the tests, and builds and starts the image, before it can be merged.
- **CI checks every locked package against known vulnerabilities** (pip-audit) on every push.

To check by hand:

```bash
uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt
uvx --from "pip-audit==2.10.1" pip-audit -r /tmp/requirements.txt --require-hashes --disable-pip
```

## What it doesn't protect against

- **Anyone who can reach it and isn't blocked by a password can read, change and download everything.** People are for "who logged this", not logins.
- **The computer it runs on.** The database and backups are plain files. Use disk encryption and keep the machine updated. Keep backups somewhere only your family can reach.
- **Reminder notifications.** On the public ntfy.sh server, anyone who knows your topic name can read the reminders. Keep the long random name private, turn off "Show medicine names", or run your own ntfy server.
- **Plain HTTP on your Wi-Fi.** On the home network without a proxy, traffic isn't encrypted. Tailscale serve or a local reverse proxy with a certificate fixes that.

## Reporting a problem

If you find a security problem, please report it privately: on GitHub, open the **Security** tab of this repository and choose **Report a vulnerability**. Please don't open a public issue, and don't include anyone's health information. I'll do my best to respond quickly, but this is a family project maintained in my spare time.
