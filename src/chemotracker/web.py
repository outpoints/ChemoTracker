"""FastAPI app: server-rendered pages, plain HTML forms, post/redirect/get."""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import re
import io
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, backup, export, history, security, icons, medinfo, profiles, reminders, rxparse, schedule, store, units
from . import symptoms as sx
from .config import PACKAGE_DIR, Settings, load_settings
from .db import MIGRATIONS, atomic, get_settings, lock_data_dir, migrate, put_settings, quick_check, schema_version, session

log = logging.getLogger("chemotracker.web")

# Set per request by the middleware: requests that change things take the database
# write lock at the start of their transaction (see db.session).
_WRITE: contextvars.ContextVar[bool] = contextvars.ContextVar("chemotracker_write", default=True)
# True while this request holds the write lock. A nested lookup (who's using this device,
# rendering a page) then reads on its own connection instead of waiting on its own lock.
_IN_WRITE: contextvars.ContextVar[bool] = contextvars.ContextVar("chemotracker_in_write", default=False)
# The person using this device, recorded in the change history for every write.
_ACTOR: contextvars.ContextVar[int | None] = contextvars.ContextVar("chemotracker_actor", default=None)

PUBLIC_PATHS = ("/static/", "/healthz", "/login", "/manifest.webmanifest")
# Pages that work before anyone on this device has said who they are.
NO_PROFILE_PATHS = PUBLIC_PATHS + ("/who", "/people", "/setup", "/logout", "/report.xlsx", "/report-",
                                   "/settings/database")


LEAD_CHOICES = ("0", "5", "10", "15", "30", "60")
NAG_CHOICES = ("0", "15", "30", "60", "90")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _reminder_values(data: dict, prefs: dict[str, str]) -> dict[str, str]:
    topic = str(data.get("ntfy_topic") or "").strip()
    server = str(data.get("ntfy_server") or reminders.DEFAULT_SERVER).strip().rstrip("/")
    app_url = str(data.get("app_url") or "").strip().rstrip("/")
    on = data.get("ntfy_enabled") == "1"
    if on and not topic:
        raise store.InvalidInput("Pick a topic name for reminders.")
    if topic and not TOPIC_RE.match(topic):
        raise store.InvalidInput("Topic names can use letters, numbers, - and _ only.")
    if not re.match(r"^https?://[^\s/]+", server):
        raise store.InvalidInput("The server should start with https:// (for example https://ntfy.sh).")
    if app_url and not re.match(r"^https?://[^\s]+$", app_url):
        raise store.InvalidInput("The link address should start with http:// or https://.")
    token = str(data.get("ntfy_token") or "").strip()
    if data.get("clear_token") == "1":
        token = ""
    elif not token:
        token = prefs.get("ntfy_token", "")  # blank field keeps the saved token
    return {
        "ntfy_enabled": "1" if on else "",
        "ntfy_topic": topic,
        "ntfy_server": server,
        "ntfy_token": token,
        "ntfy_lead": str(data.get("ntfy_lead")) if str(data.get("ntfy_lead")) in LEAD_CHOICES else "0",
        "ntfy_nag": str(data.get("ntfy_nag")) if str(data.get("ntfy_nag")) in NAG_CHOICES else "30",
        "ntfy_chemo": "1" if data.get("ntfy_chemo") == "1" else "0",
        "ntfy_names": "1" if data.get("ntfy_names") == "1" else "0",
        "app_url": app_url,
    }


def _already_text(existing: sqlite3.Row, med: sqlite3.Row, my_id: int | None) -> str:
    who = "You" if my_id is not None and my_id == (existing["updated_by"] or existing["logged_by"]) \
        else (existing["who"] or "Someone")
    if existing["status"] == "taken" and existing["taken_at"]:
        return f"{who} already marked {med['name']} taken at {schedule.fmt_time(existing['taken_at'])}."
    if existing["status"] == "taken":
        return f"{who} already marked {med['name']} given."
    return f"{who} already marked {med['name']} skipped."


def _backup_before_upgrade(cfg: Settings) -> None:
    """Copy the database before a new version changes its structure. No copy, no upgrade."""
    if not cfg.db_path.exists():
        return
    current = schema_version(cfg.db_path)
    if 0 < current < len(MIGRATIONS):
        stamp = datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
        target = backup.free_name(cfg.backup_dir, f"{backup.PREFIX}{stamp}-before-upgrade-v{current}")
        backup.snapshot(cfg, target)
        log.info("copied the database to %s before upgrading it", target)


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    cfg = settings or load_settings()
    # The real server (not tests) holds a lock so a second copy can't open the same database.
    data_lock = lock_data_dir(cfg.data_dir) if start_scheduler else None
    _backup_before_upgrade(cfg)
    migrate(cfg.db_path)
    db_health = quick_check(cfg.db_path)
    if db_health != "ok":
        log.error("database check failed: %s. Restore the newest good backup (see README).", db_health)
    schedule.set_timezone(cfg.tz)
    scheduler = backup.BackupScheduler(cfg)
    nudger = reminders.ReminderScheduler(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_scheduler:
            scheduler.start()
            nudger.start()
        yield
        scheduler.stop()
        nudger.stop()

    app = FastAPI(title="ChemoTracker", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.scheduler = scheduler
    app.state.data_lock = data_lock
    app.state.db_health = db_health
    app.state.reminders = nudger
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    _template_globals(templates)

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        _WRITE.set(request.method not in ("GET", "HEAD"))
        _ACTOR.set(request.session.get("profile_id"))
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if cfg.password and not request.url.path.startswith(PUBLIC_PATHS) and not request.session.get("auth"):
            return RedirectResponse(f"/login?{urlencode({'next': nxt})}", status_code=303)
        if request.method == "GET" and not request.url.path.startswith(NO_PROFILE_PATHS):
            with session(cfg.db_path) as conn:
                ready = bool(get_settings(conn).get("onboarded"))
                people = profiles.all_profiles(conn)
            if ready and people and request.session.get("profile_id") not in {p["id"] for p in people}:
                if len(people) == 1:
                    request.session["profile_id"] = people[0]["id"]
                else:
                    return RedirectResponse(f"/who?{urlencode({'next': nxt})}", status_code=303)
        response = await call_next(request)
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Added after the gate so it wraps it (Starlette applies middleware outermost-last).
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, same_site="lax", max_age=60 * 60 * 24 * 60,
                       session_cookie="chemotracker", https_only=cfg.secure_cookie)

    # Outermost: checks every request before anything else runs (see security.py).
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not security.host_allowed(request, cfg.allowed_hosts):
            return security.refuse(400, "Unknown address",
                                   "ChemoTracker isn't set up to answer at this address. If it's yours, add it to "
                                   "CHEMO_ALLOWED_HOSTS in the .env file and restart.")
        if request.method in security.UNSAFE_METHODS and not security.same_origin(request):
            return security.refuse(403, "Blocked a change from another site",
                                   "Changes can only be made from ChemoTracker's own pages. Go back, reload the page, "
                                   "and try again.")
        response = await call_next(request)
        for name, value in security.HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    throttle = security.LoginThrottle()
    app.state.login_throttle = throttle
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    # Asset URLs change whenever the CSS or JS changes, so phones never keep a stale look after an update.
    asset_v = hashlib.sha256(b"".join((PACKAGE_DIR / "static" / f).read_bytes()
                                      for f in ("css/app.css", "js/app.js", "js/theme.js"))).hexdigest()[:10]

    # ------------------------------------------------------------ helpers

    def now() -> datetime:
        return datetime.now(cfg.tz).replace(tzinfo=None, second=0, microsecond=0)

    def _changed_text(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
        """Shown when someone saved this entry after you opened it. Their version is on screen."""
        person = profiles.get(conn, row["updated_by"] or row["logged_by"]) if (row["updated_by"] or row["logged_by"]) else None
        who = person["name"] if person else "Someone"
        when = datetime.fromisoformat(row["updated_at"]).astimezone(cfg.tz)
        return (f"{who} changed this at {schedule.fmt_time(when)} while you had it open. This is their version now. "
                f"Make your change again if it's still needed.")

    @contextmanager
    def db():
        if not _WRITE.get() or _IN_WRITE.get():
            with session(cfg.db_path) as conn:
                yield conn
            return
        token = _IN_WRITE.set(True)
        try:
            with session(cfg.db_path, write=True, actor=_ACTOR.get()) as conn:
                yield conn
        finally:
            _IN_WRITE.reset(token)

    def me(request: Request) -> int | None:
        """The person using this device, if they still exist and are active."""
        pid = request.session.get("profile_id")
        if not pid:
            return None
        with db() as conn:
            row = profiles.get(conn, pid)
        return pid if row and row["active"] else None

    def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        with db() as conn:
            prefs = get_settings(conn)
            people = profiles.by_id(conn)
        pid = request.session.get("profile_id")
        current_person = people.get(pid) if pid else None
        current = now()
        context.setdefault("active", "")
        return templates.TemplateResponse(
            request,
            name,
            {
                "now": current,
                "today": current.date(),
                "prefs": prefs,
                "fever_f": _fever(prefs),
                "temp_unit": units.norm(prefs.get("temp_unit")),
                "fever_show": units.show(_fever(prefs), prefs.get("temp_unit", "F")),
                "flash": request.session.pop("flash", None),
                "password_on": bool(cfg.password),
                "version": __version__,
                "asset_v": asset_v,
                "db_health": app.state.db_health,
                "people": people,
                "me": current_person if current_person and current_person["active"] else None,
                **context,
            },
            status_code=status_code,
        )

    def go(url: str, flash: str | dict | None = None, request: Request | None = None) -> RedirectResponse:
        if flash is not None and request is not None:
            request.session["flash"] = flash if isinstance(flash, dict) else {"text": flash}
        return RedirectResponse(url, status_code=303)

    async def form(request: Request) -> dict[str, Any]:
        data = await request.form()
        out: dict[str, Any] = {}
        for key in data.keys():
            values = [v for v in data.getlist(key) if isinstance(v, str)]
            out[key] = values if key.endswith("[]") or len(values) > 1 else (values[0] if values else "")
        return {k.removesuffix("[]"): v for k, v in out.items()}

    def back(value: str | None, default: str = "/") -> str:
        if value and value.startswith("/") and not value.startswith("//"):
            return value
        return default

    def onboarded() -> bool:
        with db() as conn:
            return bool(get_settings(conn).get("onboarded"))

    def parse_date(value: str | None, default: date) -> date:
        try:
            return date.fromisoformat(value) if value else default
        except ValueError:
            return default

    # ------------------------------------------------------------ system

    @app.get("/healthz")
    def healthz():
        with db() as conn:
            conn.execute("SELECT 1").fetchone()
        return JSONResponse({"ok": True, "version": __version__})

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse(
            {
                "name": "ChemoTracker",
                "short_name": "ChemoTracker",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#F2F5F4",
                "theme_color": "#11616A",
                "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
            },
            media_type="application/manifest+json",
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        if not cfg.password:
            return go("/")
        return render(request, "login.html", nxt=back(next), error=None)

    @app.post("/login")
    async def login(request: Request):
        data = await form(request)
        who = request.client.host if request.client else "unknown"
        if throttle.blocked(who):
            return render(request, "login.html", status_code=429, nxt=back(data.get("next")),
                          error="Too many wrong tries. Wait 15 minutes, then try again.")
        if cfg.password and hmac.compare_digest(str(data.get("password", "")).encode(), cfg.password.encode()):
            throttle.succeeded(who)
            request.session.clear()  # a fresh session after signing in
            request.session["auth"] = True
            return go(back(data.get("next")))
        throttle.failed(who)
        return render(request, "login.html", status_code=401, nxt=back(data.get("next")), error="That password didn't match.")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return go("/login")

    # ------------------------------------------------------------ today

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request, d: str | None = None):
        if not onboarded():
            return go("/setup")
        current = now()
        day = parse_date(d, current.date())
        with db() as conn:
            prefs = get_settings(conn)
            plan = schedule.day_plan(conn, day, current)
            ring = schedule.ring(conn, plan.cycle, day, _fever(prefs)) if plan.cycle else []
            upcoming = schedule.next_cycle_after(conn, day) if plan.cycle is None else None
            nxt = schedule.next_doses(conn, current, limit=2)
            follow = schedule.follow_ups(conn, current) if day == current.date() else []
            feelings = store.symptoms_on(conn, day)
            other_prn = [
                m for m in store.medications(conn)
                if any(p["kind"] == "as_needed" for p in store.plans(conn, m["id"]))
                and m["id"] not in {a.medication_id for a in plan.as_needed}
            ]
            focus = _focus_med(plan, nxt)
            # Until noon, an unlogged bedtime dose from last night stays on Today instead of vanishing at midnight.
            overnight = None
            if day == current.date() and current.hour < 12:
                last = [s for s in schedule.day_plan(conn, day - timedelta(days=1), current).scheduled
                        if s.log is None and s.period == "bedtime"]
                if last:
                    overnight = schedule.Compartment("lastnight", "From last night", last)
            span = schedule.cycle_span(conn, plan.cycle) if plan.cycle else 0
            extras = schedule.extra_doses(conn, plan.cycle, day)
        return render(
            request, "today.html", active="today", plan=plan, ring=ring, upcoming=upcoming, next_doses=nxt, extras=extras,
            follow_ups=follow, feelings=feelings, other_prn=other_prn, focus=focus, span=span, day=day, overnight=overnight,
            upcoming_day1=schedule.day1_of(upcoming) if upcoming else None,
            quick=[sx.BY_KEY[k] for k in sx.QUICK],
        )

    def warn_hash(text: str) -> str:
        """Names the warning a confirm page showed, so "Log it anyway" only confirms that warning."""
        return hashlib.sha1(text.encode()).hexdigest()[:12]

    def confirmed(data: dict[str, Any], warning: str) -> bool:
        """True when this post answers exactly this warning. A page from before this version sends confirm=1."""
        answer = str(data.get("confirm") or "")
        return answer == "1" or (answer == "anyway" and data.get("warned") == warn_hash(warning))

    def warning_fields(data: dict[str, Any], when: datetime, warning: str, drop=("when", "at_time", "ago", "pick_time")):
        fields = {k: v for k, v in data.items() if k not in ("confirm", "warned", "replace_id", "replace_seen", *drop)}
        fields["taken_at"] = when.strftime("%Y-%m-%dT%H:%M")  # the exact time, so answering later doesn't move it
        fields["confirm"], fields["warned"] = "anyway", warn_hash(warning)
        return fields

    def remember_tap(request: Request, plan_id: int, key: str, status: str, log_id: int, stamp: str) -> None:
        """This device's recent taps, so its own double tap is recognised (and another phone's tap never is)."""
        taps = [t for t in request.session.get("taps", []) if time.time() - t[5] < 120][-9:]
        taps.append([plan_id, key, status, log_id, stamp, time.time()])
        request.session["taps"] = taps

    def repeat_tap(request: Request, slot: schedule.Slot, status: str, existing: sqlite3.Row) -> bool:
        stamp = store.dose_stamp(existing)
        return any(t[0] == slot.plan["id"] and t[1] == slot.key and t[2] == status and t[3] == existing["id"]
                   and t[4] == stamp and time.time() - t[5] < 120 for t in request.session.get("taps", []))

    def last_event(conn: sqlite3.Connection, log_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM history WHERE entity = 'dose' AND ref = ? ORDER BY id DESC LIMIT 1",
                            (str(log_id),)).fetchone()

    def logged_flash(text: str, conn: sqlite3.Connection, log_id: int, restore: int | None = None, **extra) -> dict:
        row = store.dose_log(conn, log_id)
        out = {"text": text, "undo": log_id, "undo_seen": store.dose_stamp(row) if row else "", **extra}
        if restore:
            out["undo_restore"] = restore
        return out

    def slot_name(slot: schedule.Slot) -> str:
        return slot.plan["med_name"]

    @app.post("/slot")
    async def record_slot(request: Request):
        data = await form(request)
        current = now()
        my_id = me(request)
        moved_note = ""
        with db() as conn:
            pid = int(data["plan_id"]) if str(data.get("plan_id") or "").isdigit() else None
            key = str(data.get("slot") or "")
            slot = schedule.find_slot(conn, pid, key) if pid is not None else None
            if slot is None:
                # A row from a page opened before the schedule changed. Never drop the tap: it means that plan's (or
                # that medicine's) dose on the same day, wherever its time moved.
                hint = int(data["medication_id"]) if str(data.get("medication_id") or "").isdigit() else None
                slot = schedule.find_moved_slot(conn, pid, hint, key)
                if slot is None:
                    return go(back(data.get("back")), {
                        "text": "That dose isn't on the schedule for that day any more, so nothing was logged. "
                                "If it was given, log it with Log a dose so the record is right.",
                        "link": "/dose/new", "link_text": "Log a dose"}, request)
                if slot.time and key[11:16] != slot.time:
                    moved_note = f" Its time changed to {schedule.fmt_time(slot.time)}, so this counts as that dose."
            plan_row = slot.plan
            status = data.get("status") if data.get("status") in ("taken", "skipped") else "taken"
            # Logging an earlier day's dose after the fact: default to its planned time, not "now".
            if slot.date == current.date():
                default_time = current
            elif slot.time:
                default_time = slot.dt
            else:  # clinic dose on another day from a page that didn't ask when: keep it on that day
                default_time = datetime.combine(slot.date, current.time())
            # "When was it taken?" from the Taken panel: just now or another clock time.
            choice = data.get("when")
            word = "given" if plan_row["kind"] == "clinic" else "taken"
            if choice:
                try:
                    taken_at = _chosen_time(choice, slot, data.get("at_time"), current)
                except ValueError:
                    return go(back(data.get("back")), f"Pick the time it was {word}.", request)
            else:
                try:
                    taken_at = store.parse_local_dt(data.get("taken_at"), default_time)
                except store.InvalidInput:
                    return go(back(data.get("back")), f"Pick the time it was {word}.", request)
            if status == "taken" and taken_at > current + schedule.AHEAD_OK:
                return go(back(data.get("back")), "That time hasn't happened yet. Log it after it's taken.", request)
            med = store.medication(conn, plan_row["medication_id"])
            existing = slot.log
            answer = str(data.get("confirm") or "")
            restore = None
            replaced = False
            if existing is not None and repeat_tap(request, slot, status, existing):
                # This device's double tap. The browser shows this second response, so it reads exactly like the
                # first one did, and nothing changes.
                log_id = existing["id"]
                taken_at = schedule.parse_local(existing["taken_at"]) or taken_at
            elif existing is not None and (answer == "1" or (
                    answer == "replace" and data.get("replace_id") == str(existing["id"])
                    and data.get("replace_seen") == store.dose_stamp(existing))):
                # "Replace it with mine", answering the entry the page showed (confirm=1: a page from before).
                before = conn.execute("SELECT COALESCE(MAX(id), 0) FROM history").fetchone()[0]
                log_id = store.log_slot(conn, plan_row, slot.key, status, taken_at, slot.dose or None, by=my_id,
                                        log_id=existing["id"])
                ev = conn.execute("""SELECT MAX(id) FROM history WHERE entity = 'dose' AND ref = ? AND action = 'changed'
                                       AND id > ?""", (str(log_id), before)).fetchone()[0]
                restore = ev or None
                replaced = True
            elif existing is not None and med is not None:
                # Someone recorded it after this page loaded (or before the schedule changed), or a warning page was
                # left open while it was logged. Show what's there. Only "Replace it with mine" changes it, and
                # only the entry shown here.
                fields = {k: v for k, v in data.items()
                          if k not in ("confirm", "warned", "when", "at_time", "replace_id", "replace_seen")}
                fields.update({"plan_id": str(plan_row["id"]), "slot": slot.key,
                               "taken_at": taken_at.strftime("%Y-%m-%dT%H:%M"), "confirm": "replace",
                               "replace_id": str(existing["id"]), "replace_seen": store.dose_stamp(existing)})
                return render(request, "dose_confirm.html", status_code=409, active="today", med=med,
                              title="Already recorded", when=taken_at, back_url=back(data.get("back")),
                              warning=_already_text(existing, med, my_id),
                              action="/slot", fields=fields,
                              lead="You tapped", cancel_label="Leave it as it is", cancel_primary=True,
                              confirm_label="Replace it with mine",
                              note="Only replace it if the entry above is wrong. Don't give a second dose unless "
                                   "your care team says to.")
            else:
                warning = schedule.dose_warning(conn, med, taken_at, by=my_id, slot=slot, now=current) \
                    if (status == "taken" and med is not None) else None
                if warning and not confirmed(data, warning):
                    fields = warning_fields(data, taken_at, warning)
                    fields.update({"plan_id": str(plan_row["id"]), "slot": slot.key})
                    return render(request, "dose_confirm.html", active="today", med=med, warning=warning, when=taken_at,
                                  action="/slot", fields=fields, back_url=back(data.get("back")), lead="You tapped",
                                  cancel_label="Leave it as it is", cancel_primary=True, confirm_label="Log it anyway",
                                  note="Only log it if this is a separate dose. Don't give another dose unless the "
                                       "label or your care team says to.")
                log_id = store.log_slot(conn, plan_row, slot.key, status, taken_at, slot.dose or None, by=my_id)
            row = store.dose_log(conn, log_id)
            if row is not None:
                remember_tap(request, plan_row["id"], slot.key, status, log_id, store.dose_stamp(row))
            name = med["name"] if med else "Dose"
            if status == "taken":
                text = f"{name} marked {'given' if plan_row['kind'] == 'clinic' else 'taken'} at {schedule.fmt_time(taken_at)}."
                msg = logged_flash(text + moved_note, conn, log_id, restore, logged=[log_id])
            else:
                msg = logged_flash(f"{name} marked skipped.{moved_note}", conn, log_id, restore)
            if replaced and restore is None:
                msg.pop("undo", None)  # the replace changed nothing, so Undo must not remove the entry
        return go(back(data.get("back")), msg, request)

    @app.post("/slots/taken")
    async def record_slots(request: Request):
        """"Took all" in a compartment: every open home dose that's due or past, at one chosen time.

        Each dose is written before the next one is checked, so two doses of one medicine are never logged at the
        same minute without asking. A dose whose schedule changed after the page loaded counts for its new time,
        and a dose that's no longer planned that day is named in the message, never dropped silently.
        """
        data = await form(request)
        current = now()
        my_id = me(request)
        ids: list[int] = []
        times: list[datetime] = []
        names: list[str] = []
        mine, theirs, left_out, moved, gone = 0, [], [], [], []
        with db() as conn:
            for item in _as_list(data.get("slots")):
                pid, _, key = item.partition("|")
                if not pid.isdigit():
                    continue
                slot = schedule.find_slot(conn, int(pid), key)
                if slot is None:
                    slot = schedule.find_moved_slot(conn, int(pid), None, key)
                    if slot is None:
                        old = store.plan(conn, int(pid))
                        old_med = store.medication(conn, old["medication_id"]) if old is not None else None
                        gone.append(old_med["name"] if old_med is not None else "One dose")
                        continue
                    if slot.time and key[11:16] != slot.time:
                        moved.append(f"{slot_name(slot)}'s time changed to {schedule.fmt_time(slot.time)}")
                if slot.kind == "clinic":
                    continue
                if slot.log is not None:
                    if my_id is not None and (slot.log["updated_by"] or slot.log["logged_by"]) == my_id:
                        mine += 1
                    else:
                        theirs.append(slot.log["who"] if "who" in slot.log.keys() and slot.log["who"] else "")
                    continue  # logged since the page loaded: leave it as it is
                try:
                    when = _chosen_time(data.get("when") or "now", slot, data.get("at_time"), current)
                except ValueError:
                    return go(back(data.get("back")), "Pick the time they were taken.", request)
                if when > current + schedule.AHEAD_OK:
                    return go(back(data.get("back")), "That time hasn't happened yet. Log them after they're taken.", request)
                med = store.medication(conn, slot.plan["medication_id"])
                if med is not None and schedule.dose_warning(conn, med, when, by=my_id, slot=slot, now=current):
                    left_out.append(slot_name(slot))
                    continue  # it may already be given (or be a second dose of one medicine): its own Taken asks
                ids.append(store.log_slot(conn, slot.plan, slot.key, "taken", when, slot.dose or None, by=my_id))
                times.append(when)
                names.append(slot_name(slot))
            stamps = [store.dose_stamp(store.dose_log(conn, i)) for i in ids]
        notes = ""
        if moved:
            notes += " " + ". ".join(moved) + ", so it counts as that dose."
        if left_out:
            notes += f" {' and '.join(dict.fromkeys(left_out))} left out: it may already be logged. Use its own Taken to check."
        if gone:
            notes += (f" {' and '.join(gone)} not logged: not on the schedule that day any more. "
                      "If it was given, log it with Log a dose.")
        already = ""
        if mine:
            already += f" {mine} {'was' if mine == 1 else 'were'} already logged by you."
        if theirs:
            known = [n for n in theirs if n]
            who = known[0] if known and len(set(known)) == 1 and len(known) == len(theirs) else "someone else"
            already += f" {len(theirs)} already logged by {who}."
        if not ids:
            text = ("They were already logged." if (mine or theirs) else "Nothing logged.") + notes
            return go(back(data.get("back")), text, request)
        at = f"at {schedule.fmt_time(times[0])}" if len(set(times)) == 1 else "at their planned times"
        text = f"{len(ids)} doses marked taken {at}." if len(ids) > 1 else f"{names[0]} marked taken {at}."
        return go(back(data.get("back")), {"text": text + already + notes, "undo_many": ids, "undo_seen_many": stamps,
                                           "logged": ids}, request)

    def undo_one(conn: sqlite3.Connection, request: Request, log_id: int, seen: str | None,
                 restore: str | None) -> str | None:
        """Undo one logged dose. Returns None when done, else the reason it was left as it is.

        A button carries the dose's stamp: if anything about the dose changed since the button was shown
        (someone else corrected it, or its id now belongs to another dose), nothing happens. Without a stamp
        (a page from before), only the person who made the last change can undo it. Undo after "Replace it with
        mine" puts the earlier entry back instead of deleting the dose.
        """
        row = store.dose_log(conn, log_id)
        if row is None:
            return "gone"
        my_id = me(request)
        if seen:
            if seen != store.dose_stamp(row):
                return "changed"
        else:
            ev = last_event(conn, log_id)
            if ev is not None and ev["actor"] != my_id:
                return "changed"
            if ev is None and (row["updated_by"] or row["logged_by"]) != my_id:
                return "changed"
            if ev is not None and ev["action"] == "changed" and not restore:
                restore = str(ev["id"])
        if restore and str(restore).isdigit():
            ev = conn.execute("""SELECT * FROM history WHERE id = ? AND entity = 'dose' AND ref = ? AND action = 'changed'""",
                              (int(restore), str(log_id))).fetchone()
            if ev is not None and ev["before"]:
                try:
                    store.restore_dose(conn, log_id, json.loads(ev["before"]))
                except sqlite3.IntegrityError:
                    return "changed"
                return None
        store.delete_dose_log(conn, log_id)
        return None

    def undo_refused(conn: sqlite3.Connection, log_id: int) -> dict:
        row = store.dose_log(conn, log_id)
        if row is None:
            return {"text": "That dose was already removed."}
        when = schedule.parse_local(row["taken_at"])
        what = f"{row['med_name']} {'taken' if row['status'] == 'taken' else 'skipped'}" + \
            (f" at {schedule.fmt_time(when)}" if when else "")
        person = profiles.get(conn, row["updated_by"] or row["logged_by"]) if (row["updated_by"] or row["logged_by"]) else None
        who = f", by {person['name']}" if person else ""
        return {"text": f"That dose changed after this Undo was shown, so it was left as it is. It now says {what}{who}.",
                "link": f"/dose/{log_id}", "link_text": "Check it"}

    @app.post("/doses/undo")
    async def undo_doses(request: Request):
        data = await form(request)
        ids = [int(x) for x in _as_list(data.get("ids")) if str(x).isdigit()]
        seens = _as_list(data.get("seen"))
        kept, refused = 0, None
        with db() as conn:
            for i, log_id in enumerate(ids):
                why = undo_one(conn, request, log_id, seens[i] if i < len(seens) else None, None)
                if why == "changed":
                    kept += 1
                    refused = refused or undo_refused(conn, log_id)
        if kept and kept == len(ids) and refused:
            return go(back(data.get("back")), refused, request)
        if kept:
            return go(back(data.get("back")), f"Undone. {kept} {'dose was' if kept == 1 else 'doses were'} changed after "
                                              "this Undo was shown, so they were left as they are.", request)
        return go(back(data.get("back")), "Undone.", request)

    @app.post("/dose/{log_id}/undo")
    async def undo_dose(request: Request, log_id: int):
        data = await form(request)
        with db() as conn:
            why = undo_one(conn, request, log_id, data.get("seen") or None, data.get("restore") or None)
            if why == "changed":
                return go(back(data.get("back")), undo_refused(conn, log_id), request)
        return go(back(data.get("back")), "That dose was already removed." if why == "gone" else "Undone.", request)

    @app.post("/dose/quick")
    async def quick_dose(request: Request):
        data = await form(request)
        current = now()
        with db() as conn:
            med = store.medication(conn, int(data["medication_id"]))
            if med is None:
                raise HTTPException(404)
            try:
                when = dose_time(data, current) if (data.get("ago") or data.get("taken_at")) else current
            except store.InvalidInput as exc:
                return go(back(data.get("back")), str(exc), request)
            if when > current + schedule.AHEAD_OK:
                return go(back(data.get("back")), "That time hasn't happened yet. Log it after you take it.", request)
            # Checked again when "Log it anyway" comes back, so a second tap or a dose logged since is never let through.
            warning = schedule.dose_warning(conn, med, when, by=me(request), now=current)
            if warning and not confirmed(data, warning):
                return render(request, "dose_confirm.html", active="today", med=med, warning=warning,
                              cancel_label="Leave it as it is", cancel_primary=True, confirm_label="Log it anyway",
                              action="/dose/quick", fields=warning_fields(data, when, warning), when=when,
                              back_url=back(data.get("back")))
            plan_id = int(data["plan_id"]) if str(data.get("plan_id") or "").isdigit() else None
            if plan_id is not None and (store.plan(conn, plan_id) is None or store.plan(conn, plan_id)["kind"] != "as_needed"):
                plan_id = None
            log_id = store.log_dose(conn, med["id"], when, med["dose"], data.get("reason"), plan_id, by=me(request))
            msg = logged_flash(f"{med['name']} logged at {schedule.fmt_time(when)}{_day_words(when, current)}.", conn,
                               log_id, follow=log_id, logged=[log_id])
        return go(back(data.get("back")), msg, request)

    def log_dose_context(conn: sqlite3.Connection, current: datetime) -> dict[str, Any]:
        """Everything the "Log a dose" screen needs for every active medicine, so changing the medicine
        updates the screen without another page load (and the first pick works without scripts)."""
        meds = [m for m in store.medications(conn) if m["active"]]
        plans = {m["id"]: store.plans(conn, m["id"]) for m in meds}
        home_groups, clinic = _medicine_sections(meds, plans)
        facts = {}
        for m in meds:
            an = schedule._as_needed_row(conn, m, None, current)
            is_prn = any(p["kind"] == "as_needed" for p in plans[m["id"]])
            line = ""
            if is_prn:
                line = an.status_text(current)
                if an.last_at:
                    line += f". Last one {schedule.fmt_relative_day(an.last_at.date(), current.date()).lower()} at {schedule.fmt_time(an.last_at)}"
            facts[m["id"]] = {"dose": m["dose"] or "", "directions": m["directions"] or "", "status": line,
                              "clinic_note": _clinic_note(conn, m, plans[m["id"]], current.date())}
        return {"home_groups": home_groups, "clinic": clinic, "facts": facts}

    def dose_new_context(conn: sqlite3.Connection, m: sqlite3.Row, current: datetime) -> dict[str, Any]:
        """What "Log a dose" shows: the last dose and the label's spacing, and reasons that fit this medicine."""
        is_prn = any(p["kind"] == "as_needed" for p in store.plans(conn, m["id"]))
        tags = [sx.BY_KEY[k].name for k, keys in sx.TREATED_BY.items() if m["info_key"] in keys and k in sx.BY_KEY]
        return {"an": schedule._as_needed_row(conn, m, None, current), "is_prn": is_prn, "reason_tags": tags[:4]}

    def dose_time(data: dict[str, Any], current: datetime) -> datetime:
        """The time from "When was it taken?": minutes ago, a picked clock time, or a full date and time.

        A picked clock time later than now is last evening's dose only when it's morning now and the time is in
        the evening (an 11 PM dose logged at 1 AM). Any other time later than now hasn't happened yet: it is
        refused, never quietly moved to yesterday (a slip of the time wheel would hide the dose from the gap).
        """
        ago = str(data.get("ago") or "")
        if ago.isdigit():
            return current - timedelta(minutes=int(ago))
        if ago == "pick":
            try:
                hh, mm = (int(x) for x in str(data.get("pick_time") or "").split(":")[:2])
                when = current.replace(hour=hh, minute=mm, second=0, microsecond=0)
            except ValueError:
                raise store.InvalidInput("Pick the time it was taken.") from None
            if when > current + schedule.AHEAD_OK:
                if current.hour < 12 and hh >= 17:
                    return when - timedelta(days=1)
                raise store.InvalidInput("That time hasn't happened yet. Pick the time it was taken.")
            return when
        return store.parse_local_dt(data.get("taken_at"), current)

    def dose_reason(data: dict[str, Any]) -> str | None:
        picked = [r for r in _as_list(data.get("reason")) if r and r != "other"]
        typed = store.clean(data.get("reason_text"))
        return ", ".join(picked + ([typed] if typed else [])) or None

    @app.get("/dose/new", response_class=HTMLResponse)
    def new_dose(request: Request, med: int | None = None, reason: str = "", back_to: str = "/"):
        """"Log a dose": one screen with the medicine, the dose and when. Opened with ?med= it's already picked."""
        with db() as conn:
            if med is not None and store.medication(conn, med) is None:
                raise HTTPException(404)
            ctx = log_dose_context(conn, now())
        return render(request, "dose_new.html", active="today", error=None, back_url=back(back_to), **ctx,
                      values={"medication_id": str(med or ""), "ago": "0", "reason": reason})

    @app.post("/dose/check")
    async def check_dose(request: Request):
        """For the in-place early-dose check on "Log a dose". Saving checks again on the server."""
        data = await form(request)
        current = now()
        with db() as conn:
            m = store.medication(conn, int(data.get("medication_id") or 0))
            if m is None:
                raise HTTPException(404)
            try:
                when = dose_time(data, current)
            except store.InvalidInput as exc:
                return JSONResponse({"error": str(exc)})
            if when > current + schedule.AHEAD_OK:
                return JSONResponse({"error": "That time hasn't happened yet. Log a dose after you take it."})
            warning = schedule.dose_warning(conn, m, when, by=me(request), now=current,
                                            unplanned_ok=data.get("clinic_ok") == "1")
        return JSONResponse({"warning": warning, "when": when.strftime("%Y-%m-%dT%H:%M")})

    def skipped_slot(conn: sqlite3.Connection, med_id: int, when: datetime) -> schedule.Slot | None:
        """A planned dose of this medicine marked skipped that day, already due at `when`: a dose logged later
        is most likely that dose, given late."""
        for s in schedule.slots_on(conn, when.date()):
            if s.plan["medication_id"] == med_id and s.log is not None and s.log["status"] == "skipped" \
                    and schedule.real_diff(s.dt, when) <= schedule.DUE_BEFORE:
                return s
        return None

    @app.post("/dose/new")
    async def create_dose(request: Request):
        data = await form(request)
        current = now()
        my_id = me(request)
        with db() as conn:
            if not str(data.get("medication_id") or "").isdigit():
                return render(request, "dose_new.html", status_code=400, active="today", error="Pick the medicine first.",
                              back_url=back(data.get("back")), values=data, **log_dose_context(conn, current))
            m = store.medication(conn, int(data["medication_id"]))
            if m is None:
                raise HTTPException(404)
            reason = dose_reason(data)
            answer = str(data.get("confirm") or "")
            try:
                when = dose_time(data, current)
                if when > current + schedule.AHEAD_OK:
                    raise store.InvalidInput("That time hasn't happened yet. Log a dose after you take it.")
                base = {"medication_id": str(m["id"]), "taken_at": when.strftime("%Y-%m-%dT%H:%M"),
                        "dose": data.get("dose") or "", "reason_text": reason or "", "back": data.get("back") or "/"}
                if data.get("clinic_ok") == "1":
                    base["clinic_ok"] = "1"
                # A clinic medicine on a day it isn't given: the pop-up asked already (clinic_ok). Without scripts, ask here.
                note = _clinic_note(conn, m, store.plans(conn, m["id"]), when.date())
                if note and data.get("clinic_ok") != "1" and answer != "1":
                    return render(request, "dose_confirm.html", active="today", med=m, warning=note, when=when,
                                  title="Given at the clinic", action="/dose/new", fields={**base, "clinic_ok": "1"},
                                  back_url="/dose/new", cancel_label="Pick another medicine", cancel_primary=True,
                                  confirm_label="Log it anyway",
                                  note="Only log it if it was given and nobody has logged it yet.")
                # A planned dose marked skipped that day: this is most likely that dose, given late. Ask, so the day
                # doesn't show it skipped plus a "More than planned" dose.
                late = None
                if str(data.get("as_skipped") or "").isdigit():
                    late = skipped_slot(conn, m["id"], when)
                    if late is None or late.log["id"] != int(data["as_skipped"]) or \
                            store.dose_stamp(late.log) != data.get("skipped_seen"):
                        late = None
                        answer = ""  # it changed since the question: ask again below
                target = None if late is not None else schedule.slot_for_new_dose(conn, m["id"], when)
                if late is None and target is None and answer not in ("1", "anyway", "separate"):
                    was = skipped_slot(conn, m["id"], when)
                    if was is not None:
                        who = schedule._who_logged(was.log, my_id)
                        who = "You" if who == "you" else who
                        at = schedule.fmt_time(was.time) if was.time else "planned"
                        return render(request, "dose_confirm.html", active="today", med=m, when=when,
                                      warning=f"{who} marked the {at} dose skipped. If this is that dose, given "
                                              "later, log it as that dose so the day shows one dose.",
                                      action="/dose/new", fields={**base, "as_skipped": str(was.log["id"]),
                                                                  "skipped_seen": store.dose_stamp(was.log)},
                                      back_url=back(data.get("back")), cancel_label="Leave it as it is",
                                      cancel_primary=True, confirm_label=f"Log it as the {at} dose",
                                      alt_fields={**base, "confirm": "separate"}, alt_label="It's a separate dose",
                                      note="If you're not sure whether it was given, check with whoever gave it first.")
                warning = schedule.dose_warning(conn, m, when, by=my_id, now=current, slot=late,
                                                unplanned_ok=data.get("clinic_ok") == "1",
                                                exclude_id=late.log["id"] if late is not None else None)
                if warning and not confirmed(data, warning):
                    fields = {**base, "confirm": "anyway", "warned": warn_hash(warning)}
                    if late is not None:
                        fields.update({"as_skipped": str(late.log["id"]), "skipped_seen": store.dose_stamp(late.log)})
                    return render(request, "dose_confirm.html", active="today", med=m, warning=warning,
                                  cancel_label="Leave it as it is", cancel_primary=True, confirm_label="Log it anyway",
                                  action="/dose/new", fields=fields, when=when, back_url=back(data.get("back")))
                dose = data.get("dose") or m["dose"]
                if late is not None:
                    log_id = store.log_slot(conn, late.plan, late.key, "taken", when, store.clean(dose), by=my_id,
                                            log_id=late.log["id"])
                    if reason:
                        conn.execute("UPDATE dose_logs SET reason = ? WHERE id = ?", (reason, log_id))
                elif target is not None:
                    # The dose fills a planned dose: record which one, so every screen and report agrees.
                    log_id = store.log_slot(conn, target.plan, target.key, "taken", when, store.clean(dose), by=my_id,
                                            reason=reason)
                else:
                    log_id = store.log_dose(conn, m["id"], when, dose, reason, by=my_id)
                asks = schedule.asks_if_it_helped(conn, store.dose_log(conn, log_id))
                msg = logged_flash(f"{m['name']} logged at {schedule.fmt_time(when)}{_day_words(when, current)}.", conn,
                                   log_id, follow=log_id if asks else None, logged=[log_id])
            except store.InvalidInput as exc:
                return render(request, "dose_new.html", status_code=400, active="today", error=str(exc),
                              back_url=back(data.get("back")), values=data, **log_dose_context(conn, current))
        return go(back(data.get("back")), msg, request)

    @app.get("/dose/{log_id}", response_class=HTMLResponse)
    def dose_page(request: Request, log_id: int, saved: int = 0, back_to: str = "/"):
        with db() as conn:
            row = store.dose_log(conn, log_id)
            if row is None:
                raise HTTPException(404)
            events = history.events(conn, cfg.tz, "dose", log_id)
            asks = schedule.asks_if_it_helped(conn, row)
        return render(request, "dose.html", active="today", log=row, saved=bool(saved), error=None, asks=asks,
                      chips=medinfo.chips_for(row["med_info"]), info=medinfo.lookup(row["med_info"]), events=events,
                      back_url=back(back_to))

    def fits_its_slot(row: sqlite3.Row, when: datetime) -> bool:
        """Whether a dose logged for a planned dose still belongs to it at this time: the same day, the next morning
        for an evening or bedtime dose, or the small hours after an afternoon dose (the rule Taken's clock uses)."""
        if not row["slot"]:
            return True
        d = date.fromisoformat(row["slot"][:10])
        if when.date() == d:
            return True
        planned = row["slot"][11:16] if len(row["slot"]) > 10 else ""
        return when.date() == d + timedelta(days=1) and ((planned >= "17:00" and when.hour < 12)
                                                         or (planned >= "12:00" and when.hour < 5))

    @app.post("/dose/{log_id}")
    async def save_dose(request: Request, log_id: int):
        data = await form(request)
        current = now()
        my_id = me(request)

        def again(conn, error, status_code=400):
            row = store.dose_log(conn, log_id)
            return render(request, "dose.html", status_code=status_code, active="today", log=row, saved=False,
                          asks=schedule.asks_if_it_helped(conn, row), back_url=back(data.get("back")), error=error,
                          chips=medinfo.chips_for(row["med_info"]), info=medinfo.lookup(row["med_info"]))

        with db() as conn:
            row = store.dose_log(conn, log_id)
            if row is None:
                raise HTTPException(404)
            if data.get("seen") and data["seen"] != row["updated_at"]:
                return again(conn, _changed_text(conn, row), 409)
            status = data.get("status") or "taken"
            rebind = None
            if status == "taken":
                raw = str(data.get("taken_at") or "").strip()
                if not raw:
                    return again(conn, "Pick the time it was taken. The time can't be left empty.")
                try:
                    when = store.parse_local_dt(raw, current)
                except store.InvalidInput as exc:
                    return again(conn, str(exc))
                if when > current + schedule.AHEAD_OK:
                    return again(conn, "That time hasn't happened yet. Pick the time it was taken.")
                old = schedule.parse_local(row["taken_at"])
                moved_day = old is None or when.date() != old.date()
                if row["slot"] and not fits_its_slot(row, when):
                    # Moved to another day: it no longer counts for the planned dose it was logged for. It counts for
                    # the planned dose it fits on its new day, or none, so that day can't offer a second one.
                    target = schedule.slot_for_new_dose(conn, row["medication_id"], when)
                    rebind = (target.plan["id"], target.key, target.kind) if target is not None \
                        else (None, None, row["kind"] if row["kind"] != "as_needed" else "scheduled")
                if moved_day:
                    med = store.medication(conn, row["medication_id"])
                    # Only the double-dose checks: whether that day was a planned day isn't the question here.
                    warning = schedule.dose_warning(conn, med, when, by=my_id, now=current, exclude_id=log_id,
                                                    unplanned_ok=True) if med is not None else None
                    if warning and not confirmed(data, warning):
                        fields = {k: v for k, v in data.items() if k not in ("confirm", "warned")}
                        fields.update({"confirm": "anyway", "warned": warn_hash(warning)})
                        return render(request, "dose_confirm.html", active="today", med=med, warning=warning, when=when,
                                      action=f"/dose/{log_id}", fields=fields, back_url=f"/dose/{log_id}",
                                      lead="You're moving this dose to", cancel_label="Leave it as it is",
                                      cancel_primary=True, confirm_label="Save it anyway",
                                      note="Only save it if this is the time it was really given.")
            try:
                store.update_dose_log(conn, log_id, data, current, by=my_id, rebind=rebind)
            except store.InvalidInput as exc:
                return again(conn, str(exc))
        return go(back(data.get("back")), "Saved.", request)

    @app.post("/dose/{log_id}/follow")
    async def follow_dose(request: Request, log_id: int):
        data = await form(request)
        with db() as conn:
            row = store.dose_log(conn, log_id)
            # The question was about one dose. If that dose was removed or changed since (its id can even be reused
            # by another dose), the answer must not land on something else. Pages send the dose's stamp. Without
            # one (a page from before), an id that was ever freed and reused is too uncertain to write to.
            reused = conn.execute("SELECT 1 FROM history WHERE entity = 'dose' AND ref = ? AND action = 'deleted' LIMIT 1",
                                  (str(log_id),)).fetchone() is not None
            if row is None or row["status"] != "taken" or (data.get("seen") and data["seen"] != store.dose_stamp(row)) \
                    or (not data.get("seen") and reused):
                return go(back(data.get("back")), "That dose was changed or removed, so the answer wasn't saved.",
                          request)
            store.quick_follow_up(conn, log_id, data.get("helped"), data.get("dose_feel"), by=me(request))
        return go(back(data.get("back")), "Thanks, noted.", request)

    @app.post("/dose/{log_id}/delete")
    async def delete_dose(request: Request, log_id: int):
        data = await form(request)
        with db() as conn:
            row = store.dose_log(conn, log_id)
            if row is not None and data.get("seen") and data["seen"] not in (row["updated_at"], store.dose_stamp(row)):
                return render(request, "dose.html", status_code=409, active="today", log=row, saved=False,
                              asks=schedule.asks_if_it_helped(conn, row), back_url=back(data.get("back")),
                              error=_changed_text(conn, row), chips=medinfo.chips_for(row["med_info"]),
                              info=medinfo.lookup(row["med_info"]))
            store.delete_dose_log(conn, log_id)
        return go(back(data.get("back")), "Dose removed.", request)

    # ------------------------------------------------------------ symptoms

    @app.get("/symptoms", response_class=HTMLResponse)
    def symptoms_page(request: Request, d: str | None = None):
        current = now()
        day = parse_date(d, current.date())
        with db() as conn:
            entries = store.symptoms_on(conn, day)
            doses = [r for r in store.doses_between(conn, day, day) if r["status"] == "taken"]
            week = schedule.symptoms_between(conn, day - timedelta(days=6), day)
            cycle = schedule.cycle_for_date(conn, day)
            fever = _fever(get_settings(conn))
        timeline = sorted(
            [("symptom", e["logged_at"], e) for e in entries] + [("dose", r["taken_at"], r) for r in doses],
            key=lambda t: t[1] or "", reverse=True,
        )
        return render(request, "symptoms.html", active="symptoms", day=day, timeline=timeline, entries=entries,
                      week=_week_summary(week, day, fever), cycle=cycle,
                      all_symptoms=sx.SYMPTOMS, quick=[sx.BY_KEY[k] for k in sx.QUICK])

    @app.get("/symptoms/new", response_class=HTMLResponse)
    def new_symptom(request: Request, s: str = "nausea"):
        sym = sx.lookup(s)
        return render(request, "symptom_form.html", active="symptoms", sym=sym, entry=None, error=None,
                      values={"logged_at": now().strftime("%Y-%m-%dT%H:%M")}, all_symptoms=sx.SYMPTOMS)

    @app.post("/symptoms/new")
    async def create_symptom(request: Request):
        data = await form(request)
        try:
            with db() as conn:
                entry_id = store.add_symptom(conn, data, now(), by=me(request))
        except store.InvalidInput as exc:
            return render(request, "symptom_form.html", status_code=400, active="symptoms",
                          sym=sx.lookup(data.get("symptom")), entry=None, error=str(exc), values=data,
                          all_symptoms=sx.SYMPTOMS)
        return go(f"/symptoms/{entry_id}/saved")

    @app.get("/symptoms/{entry_id}/saved", response_class=HTMLResponse)
    def symptom_saved(request: Request, entry_id: int):
        current = now()
        with db() as conn:
            entry = store.symptom_log(conn, entry_id)
            if entry is None:
                raise HTTPException(404)
            prefs = get_settings(conn)
            meds = schedule.as_needed_for_symptom(conn, entry["symptom"], current)
        sym = sx.lookup(entry["symptom"])
        concerning = sx.is_concerning(sym, entry["severity"], entry["value"], _fever(prefs))
        return render(request, "symptom_saved.html", active="symptoms", entry=entry, sym=sym, meds=meds,
                      concerning=concerning)

    @app.get("/symptoms/{entry_id}", response_class=HTMLResponse)
    def edit_symptom(request: Request, entry_id: int):
        with db() as conn:
            entry = store.symptom_log(conn, entry_id)
            if entry is None:
                raise HTTPException(404)
            events = history.events(conn, cfg.tz, "symptom", entry_id)
        values = dict(entry)
        values["logged_at"] = entry["logged_at"].replace(" ", "T")
        return render(request, "symptom_form.html", active="symptoms", sym=sx.lookup(entry["symptom"]), entry=entry,
                      error=None, values=values, all_symptoms=sx.SYMPTOMS, events=events)

    @app.post("/symptoms/{entry_id}")
    async def save_symptom(request: Request, entry_id: int):
        data = await form(request)
        with db() as conn:
            entry = store.symptom_log(conn, entry_id)
            if entry is None:
                raise HTTPException(404)
            if data.get("seen") and data["seen"] != entry["updated_at"]:
                values = dict(entry)
                values["logged_at"] = entry["logged_at"].replace(" ", "T")
                return render(request, "symptom_form.html", status_code=409, active="symptoms",
                              sym=sx.lookup(entry["symptom"]), entry=entry, error=_changed_text(conn, entry),
                              values=values, all_symptoms=sx.SYMPTOMS)
        try:
            with db() as conn:
                store.update_symptom(conn, entry_id, data, now(), by=me(request))
        except store.InvalidInput as exc:
            with db() as conn:
                entry = store.symptom_log(conn, entry_id)
            return render(request, "symptom_form.html", status_code=400, active="symptoms",
                          sym=sx.lookup(data.get("symptom")), entry=entry, error=str(exc), values=data,
                          all_symptoms=sx.SYMPTOMS)
        day = str(data.get("logged_at", ""))[:10]
        return go(f"/symptoms?d={day}" if day else "/symptoms", "Saved.", request)

    @app.post("/symptoms/{entry_id}/delete")
    async def delete_symptom(request: Request, entry_id: int):
        data = await form(request)
        with db() as conn:
            store.delete_symptom(conn, entry_id)
        return go(back(data.get("back"), "/symptoms"), "Entry removed.", request)

    # ------------------------------------------------------------ medicines

    @app.get("/medicines", response_class=HTMLResponse)
    def medicines_page(request: Request):
        with db() as conn:
            meds = store.medications(conn, active=None)
            plans = {m["id"]: store.plans(conn, m["id"]) for m in meds}
            current = now()
            available = {a.medication_id: a for m in meds for a in [schedule._as_needed_row(conn, m, None, current)]}
        home_groups, clinic = _medicine_sections([m for m in meds if m["active"]], plans)
        return render(request, "medicines.html", active="medicines", meds=meds, plans=plans, available=available,
                      home_groups=home_groups, clinic=clinic)

    @app.get("/medicines/add", response_class=HTMLResponse)
    def add_medicine_page(request: Request):
        with db() as conn:
            taken = {m["info_key"] for m in store.medications(conn, active=True) if m["info_key"]}
            rx = _rx_context(conn, "", now().date())
        return render(request, "medicine_add.html", active="medicines", taken=taken, chosen=set(), error=None,
                      catalog=medinfo.by_category(), shortcuts=medinfo.REGIMEN_SHORTCUTS, **rx)

    @app.post("/rx/preview", response_class=HTMLResponse)
    async def rx_preview(request: Request):
        data = await form(request)
        with db() as conn:
            rx = _rx_context(conn, str(data.get("rx_text") or ""), now().date())
        return templates.TemplateResponse(request, "_rx_preview.html", {**rx, "today": now().date()})

    @app.post("/medicines/add")
    async def add_medicine(request: Request):
        data = await form(request)
        keys = _as_list(data.get("keys"))
        customs = [c for c in (store.clean(x) for x in _as_list(data.get("custom_name"))) if c]
        created = []
        with db() as conn:
            if data.get("action") == "preview":
                taken = {m["info_key"] for m in store.medications(conn, active=True) if m["info_key"]}
                rx = _rx_context(conn, str(data.get("rx_text") or ""), now().date())
                chosen = set(keys) | {m.info_key for m in rx["rx_meds"] if m.info_key}
                return render(request, "medicine_add.html", active="medicines", taken=taken, chosen=chosen, error=None,
                              catalog=medinfo.by_category(), shortcuts=medinfo.REGIMEN_SHORTCUTS, **rx)
            imported = _import_rx(conn, data)
            created.extend(imported)
            rx_keys = {store.medication(conn, i)["info_key"] for i in imported}
            for key in keys:
                if key not in rx_keys:
                    created.append(store.add_from_catalog(conn, key))
            for custom in customs:
                med_id = store.create_medication(conn, {"name": custom, "route": "oral", "category": "custom"})
                store.create_plan(conn, med_id, {"kind": "as_needed", "start_day": "1", "end_day": ""})
                created.append(med_id)
        if not created:
            return go("/medicines/add", "Tick at least one medicine, or type one that isn't listed.", request)
        if len(created) == 1:
            return go(f"/medicines/{created[0]}?new=1",
                      "Added from your prescription. Check the details." if imported else "Added. Now copy the pharmacy label.",
                      request)
        note = f" {len(imported)} filled in from your prescriptions." if imported else ""
        return go("/medicines", f"Added {len(created)} medicines.{note} Open each one to check it.", request)

    @app.post("/medicines/{med_id}/import")
    async def import_into_medicine(request: Request, med_id: int):
        data = await form(request)
        parsed = rxparse.parse(str(data.get("rx_text") or ""))
        if not parsed:
            return go(f"/medicines/{med_id}?edit=1", "Couldn't read that. Type the directions from the label.", request)
        with db() as conn:
            if store.medication(conn, med_id) is None:
                raise HTTPException(404)
            store.import_parsed(conn, parsed[0], into=med_id)
        extra = " Only the first medicine was used." if len(parsed) > 1 else ""
        return go(f"/medicines/{med_id}", f"Filled in from the label. Check the details below.{extra}", request)

    @app.get("/medicines/{med_id}", response_class=HTMLResponse)
    def medicine_page(request: Request, med_id: int, new: int = 0, edit: int = 0):
        with db() as conn:
            med = store.medication(conn, med_id)
            if med is None:
                raise HTTPException(404)
            plans = store.plans(conn, med_id)
            recent = store.recent_doses(conn, med_id, 12)
            cycles = schedule.all_cycles(conn)
            length = max([c["length_days"] for c in cycles] + [14])
            events = history.for_medicine(conn, cfg.tz, med_id)
        return render(request, "medicine.html", active="medicines", med=med, plans=plans, recent=recent,
                      info=medinfo.lookup(med["info_key"]), is_new=bool(new), edit=bool(edit), error=None, length=length,
                      events=events)

    @app.post("/medicines/{med_id}")
    async def save_medicine(request: Request, med_id: int):
        data = await form(request)
        try:
            with db() as conn:
                store.update_medication(conn, med_id, data)
        except store.InvalidInput as exc:
            return go(f"/medicines/{med_id}?edit=1", str(exc), request)
        return go(back(data.get("back"), f"/medicines/{med_id}"), "Saved.", request)

    @app.post("/medicines/{med_id}/active")
    async def toggle_medicine(request: Request, med_id: int):
        data = await form(request)
        active = data.get("active") == "1"
        with db() as conn:
            store.set_active(conn, med_id, active)
        return go(f"/medicines/{med_id}", "Back on the list." if active else "Stopped. History is kept.", request)

    @app.post("/medicines/{med_id}/delete")
    async def remove_medicine(request: Request, med_id: int):
        with db() as conn:
            store.delete_medication(conn, med_id)
        return go("/medicines", "Medicine and its history deleted.", request)

    def duplicate_note(conn: sqlite3.Connection, med_id: int, data: dict, plan_id: int | None = None) -> str | None:
        """Why to pause before saving a schedule that repeats another schedule of the same medicine at the same
        time (a form sent twice, for example): Today would show the dose twice, which invites a second dose."""
        try:
            new = store.validate_plan(data)
        except store.InvalidInput:
            return None  # saving explains what's wrong
        if new["kind"] != "scheduled":
            return None
        times = set(t for t in new["times"].split(",") if t)
        med = store.medication(conn, med_id)
        for p in store.plans(conn, med_id):
            if p["id"] == plan_id or p["kind"] != "scheduled":
                continue
            same = sorted(times & set(schedule.plan_times(p)))
            days = p["start_day"] <= (new["end_day"] if new["end_day"] is not None else 999) and \
                new["start_day"] <= (p["end_day"] if p["end_day"] is not None else 999)
            cycles = p["from_cycle"] <= (new["to_cycle"] or 999) and new["from_cycle"] <= (p["to_cycle"] or 999)
            if same and days and cycles:
                at = " and ".join(schedule.fmt_time(t) for t in same)
                return (f"{med['name'] if med else 'This medicine'} already has a schedule at {at} on "
                        f"{schedule.days_text(p['start_day'], p['end_day'])}. Two schedules at the same time show the "
                        "dose twice on Today, which can lead to a second dose.")
        return None

    def duplicate_page(request: Request, conn: sqlite3.Connection, med_id: int, data: dict, note: str, action: str):
        return render(request, "dose_confirm.html", active="medicines", med=store.medication(conn, med_id), when=None,
                      title="Check this schedule", warning=note, action=action,
                      fields={**{k: v for k, v in data.items() if k != "confirm"}, "confirm": "duplicate"},
                      back_url=f"/medicines/{med_id}?edit=1#plans", cancel_label="Leave it as it is",
                      cancel_primary=True, confirm_label="Save it anyway",
                      note="Only save it if it's a separate dose, like a second tablet strength at the same time.")

    @app.post("/medicines/{med_id}/plans")
    async def add_plan(request: Request, med_id: int):
        data = await form(request)
        try:
            with db() as conn:
                if store.medication(conn, med_id) is None:
                    raise HTTPException(404)
                note = duplicate_note(conn, med_id, data) if data.get("confirm") != "duplicate" else None
                if note:
                    return duplicate_page(request, conn, med_id, data, note, f"/medicines/{med_id}/plans")
                store.create_plan(conn, med_id, data)
        except store.InvalidInput as exc:
            return go(f"/medicines/{med_id}?edit=1#plans", str(exc), request)
        return go(f"/medicines/{med_id}", "Schedule added.", request)

    @app.post("/plans/{plan_id}")
    async def save_plan(request: Request, plan_id: int):
        data = await form(request)
        with db() as conn:
            row = store.plan(conn, plan_id)
            if row is None:
                raise HTTPException(404)
            med_id = row["medication_id"]
            note = duplicate_note(conn, med_id, data, plan_id) if data.get("confirm") != "duplicate" else None
            if note:
                return duplicate_page(request, conn, med_id, data, note, f"/plans/{plan_id}")
            try:
                store.update_plan(conn, plan_id, data)
            except store.InvalidInput as exc:
                return go(f"/medicines/{med_id}?edit=1#plans", str(exc), request)
            current = now()
            kept = [s.log for s in schedule.slots_on(conn, current.date())
                    if s.plan["medication_id"] == med_id and s.log is not None and s.log["status"] == "taken"]
        note = ""
        if kept:
            times = " and ".join(schedule.fmt_time(log["taken_at"]) for log in kept if log["taken_at"])
            note = f" The dose logged today at {times} still counts." if len(kept) == 1 else f" The doses logged today at {times} still count."
        return go(f"/medicines/{med_id}", "Schedule saved." + note, request)

    @app.post("/plans/{plan_id}/delete")
    async def remove_plan(request: Request, plan_id: int):
        with db() as conn:
            row = store.plan(conn, plan_id)
            if row is None:
                raise HTTPException(404)
            store.delete_plan(conn, plan_id)
        return go(f"/medicines/{row['medication_id']}?edit=1#plans", "Schedule removed.", request)

    # ------------------------------------------------------------ cycles

    @app.get("/cycles", response_class=HTMLResponse)
    def cycles_page(request: Request):
        current = now()
        with db() as conn:
            cycles = schedule.all_cycles(conn)
            cur = schedule.cycle_for_date(conn, current.date())
            defaults = store.next_cycle_defaults(conn, current.date())
            prefs = get_settings(conn)
            ring = schedule.ring(conn, cur, current.date(), _fever(prefs)) if cur else []
        return render(request, "cycles.html", active="cycles", cycles=cycles, current=cur, defaults=defaults,
                      ring=ring, error=None)

    @app.post("/cycles")
    async def create_cycle(request: Request):
        data = await form(request)
        try:
            with db() as conn:
                ids = store.create_cycle_series(conn, data)
        except store.InvalidInput as exc:
            return go("/cycles", str(exc), request)
        if len(ids) > 1:
            return go("/cycles", f"Added {len(ids)} cycles. Change any date if treatment moves.", request)
        return go(f"/cycles/{ids[0]}", "Cycle added. Medicines follow their schedules from its Day 1.", request)

    @app.get("/cycles/{cycle_id}", response_class=HTMLResponse)
    def cycle_page(request: Request, cycle_id: int, day: int | None = None, change: int = 0):
        current = now()
        with db() as conn:
            c = store.cycle(conn, cycle_id)
            if c is None:
                raise HTTPException(404)
            days, med_rows, sym_rows = schedule.grid(conn, c, current)
            prefs = get_settings(conn)
            ring = schedule.ring(conn, c, current.date(), _fever(prefs))
            cycles = schedule.all_cycles(conn)
        today_n = schedule.day_number(c, current.date())
        if day is None or day not in days:
            day = today_n if today_n in days else 1
        day_items = [(r, r.cells[day]) for r in med_rows
                     if r.cells[day].planned or r.cells[day].taken or r.cells[day].skipped or r.cells[day].allowed]
        day_symptoms = [(r, r.cells[day]) for r in sym_rows if r.cells[day]]
        return render(request, "cycle.html", active="cycles", cycle=c, days=days, med_rows=med_rows,
                      sym_rows=sym_rows, ring=ring, cycles=cycles, score=schedule.symptom_score, sel=day,
                      sel_date=schedule.date_of(c, day), day_items=day_items, day_symptoms=day_symptoms,
                      change=bool(change), later=sum(1 for x in cycles if x["number"] > c["number"]))

    @app.post("/cycles/{cycle_id}")
    async def save_cycle(request: Request, cycle_id: int):
        data = await form(request)
        moved_note = ""
        try:
            with db() as conn:
                before = store.cycle(conn, cycle_id)
                if before is None:
                    raise HTTPException(404)
                store.update_cycle(conn, cycle_id, data)
                after = store.cycle(conn, cycle_id)
                delta = (date.fromisoformat(after["day1"]) - date.fromisoformat(before["day1"])).days
                if delta and data.get("shift_later"):
                    n = store.shift_later_cycles(conn, after["number"], delta)
                    if n:
                        moved_note = f" The {n} {'cycle' if n == 1 else 'cycles'} after it moved {abs(delta)} " \
                                     f"{'day' if abs(delta) == 1 else 'days'} {'later' if delta > 0 else 'earlier'} too."
        except store.InvalidInput as exc:
            return go(f"/cycles/{cycle_id}?change=1", str(exc), request)
        return go(f"/cycles/{cycle_id}", "Cycle saved." + moved_note, request)

    @app.post("/cycles/{cycle_id}/delete")
    async def remove_cycle(request: Request, cycle_id: int):
        with db() as conn:
            store.delete_cycle(conn, cycle_id)
        return go("/cycles", "Cycle deleted. Dose and symptom history is kept.", request)

    # ------------------------------------------------------------ report

    def report_range(conn: sqlite3.Connection, cycle_id: str | None, start: str | None, end: str | None):
        current = now().date()
        if cycle_id:
            c = store.cycle(conn, int(cycle_id))
            if c:
                first = schedule.date_of(c, 0)
                last = schedule.date_of(c, schedule.cycle_span(conn, c))
                return first, (min(last, current) if first <= current else last), c
        s = parse_date(start, current - timedelta(days=13))
        e = parse_date(end, current)
        if e < s:
            s, e = e, s
        return s, e, None

    @app.get("/report", response_class=HTMLResponse)
    def report_page(request: Request, cycle: str | None = None, start: str | None = None, end: str | None = None):
        current = now()
        day = current.date()
        with db() as conn:
            cycles = schedule.all_cycles(conn)
            this_cycle = schedule.cycle_for_date(conn, day)
            if cycle is None and start is None:
                cycle = str(this_cycle["id"]) if this_cycle else None
            s, e, c = report_range(conn, cycle, start, end)
            prefs = get_settings(conn)
            doses = export.dose_rows(conn, s, e, current)
            syms = export.symptom_rows(conn, s, e, _fever(prefs))
        query = urlencode({"cycle": c["id"]} if c else {"start": s.isoformat(), "end": e.isoformat()})
        # Which quick choice is showing, so it can be marked. Anything else opens "Another cycle or dates".
        if c is not None:
            period = "this-cycle" if this_cycle is not None and c["id"] == this_cycle["id"] else "other-cycle"
        elif e == day and s == day - timedelta(days=6):
            period = "last-7"
        elif e == day and s == day - timedelta(days=29):
            period = "last-30"
        else:
            period = "dates"
        quick = {"last-7": urlencode({"start": (day - timedelta(days=6)).isoformat(), "end": day.isoformat()}),
                 "last-30": urlencode({"start": (day - timedelta(days=29)).isoformat(), "end": day.isoformat()})}
        return render(request, "report.html", active="report", cycles=cycles, cycle=c, this_cycle=this_cycle,
                      start=s, end=e, doses=doses, syms=syms, query=query, period=period, quick=quick)

    def _download(data: bytes, filename: str, media: str) -> Response:
        return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/report.xlsx")
    def report_xlsx(cycle: str | None = None, start: str | None = None, end: str | None = None):
        current = now()
        with db() as conn:
            s, e, c = report_range(conn, cycle, start, end)
            data = export.build_workbook(conn, s, e, current)
        name = f"chemotracker-cycle-{c['number']}.xlsx" if c else f"chemotracker-{s}-to-{e}.xlsx"
        return _download(data, name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/report-doses.csv")
    def report_doses_csv(cycle: str | None = None, start: str | None = None, end: str | None = None):
        with db() as conn:
            s, e, _ = report_range(conn, cycle, start, end)
            data = export.doses_csv(conn, s, e, now())
        return _download(data, f"chemotracker-doses-{s}-to-{e}.csv", "text/csv; charset=utf-8")

    @app.get("/report-symptoms.csv")
    def report_symptoms_csv(cycle: str | None = None, start: str | None = None, end: str | None = None):
        with db() as conn:
            s, e, _ = report_range(conn, cycle, start, end)
            data = export.symptoms_csv(conn, s, e, _fever(get_settings(conn)))
        return _download(data, f"chemotracker-symptoms-{s}-to-{e}.csv", "text/csv; charset=utf-8")

    # ------------------------------------------------------------ settings

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        with db() as conn:
            backup_log = conn.execute("SELECT * FROM backups ORDER BY id DESC LIMIT 8").fetchall()
            changes = history.count(conn)
            # The name shown in the setup steps must not change if the page reloads while someone is
            # off installing ntfy, so keep the first one until reminders are saved.
            prefs_now = get_settings(conn)
            draft = prefs_now.get("ntfy_topic_draft") or ""
            if not prefs_now.get("ntfy_topic") and not draft:
                draft = reminders.new_topic()
                put_settings(conn, {"ntfy_topic_draft": draft})
        files = backup.backup_files(cfg)[:12]
        return render(request, "settings.html", active="settings", history=backup_log, files=files, cfg=cfg,
                      next_backup=scheduler.next_at or backup.next_run(cfg, now()), changes=changes,
                      nudger=nudger, suggested_topic=draft or reminders.new_topic(), roles=profiles.ROLES,
                      suggested_url=str(request.base_url).rstrip("/"))

    # ------------------------------------------------------------ reminders

    @app.post("/settings/reminders")
    async def save_reminders(request: Request):
        data = await form(request)
        with db() as conn:
            prefs = get_settings(conn)
        try:
            values = _reminder_values(data, prefs)
        except store.InvalidInput as exc:
            return go("/settings#reminders", str(exc), request)
        with db() as conn:
            put_settings(conn, values)
        msg = "Reminders are on. Send a test to check your phone." if values["ntfy_enabled"] == "1" else "Reminders are off."
        return go("/settings#reminders", msg, request)

    @app.post("/settings/reminders/test")
    async def test_reminders(request: Request):
        with db() as conn:
            prefs = get_settings(conn)
        if not prefs.get("ntfy_topic"):
            return go("/settings#reminders", "Save a topic first.", request)
        try:
            await run_in_threadpool(reminders.send, prefs, reminders.test_reminder(prefs))
        except Exception as exc:  # noqa: BLE001 - shown to the person who tapped Test
            return go("/settings#reminders", f"The test didn't go through: {exc}", request)
        return go("/settings#reminders", f"Test sent to {prefs['ntfy_topic']}. It should be on your phone now.", request)

    # ------------------------------------------------------------ restore

    @app.get("/settings/restore", response_class=HTMLResponse)
    def restore_page(request: Request, file: str = ""):
        try:
            path = backup.find(cfg, file)
            info = backup.inspect(path)
        except backup.RestoreError as exc:
            return go("/settings#backups", str(exc), request)
        with db() as conn:
            current = {t: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] for t, tbl in
                       (("doses", "dose_logs"), ("symptoms", "symptom_logs"), ("medicines", "medications"), ("cycles", "cycles"))}
        return render(request, "restore.html", active="settings", file=backup.describe(path), info=info, current=current)

    @app.post("/settings/restore")
    async def restore_now(request: Request):
        data = await form(request)
        try:
            path = backup.find(cfg, str(data.get("file") or ""))
            safety = backup.restore(cfg, path, _ACTOR.get())
        except backup.RestoreError as exc:
            return go("/settings#backups", str(exc), request)
        app.state.db_health = quick_check(cfg.db_path)
        when = backup.describe(path)
        label = f"{schedule.fmt_day(when.taken.date())} {schedule.fmt_time(when.taken)}" if when else path.name
        return go("/", f"Restored the backup from {label}. What was here before is saved as "
                       f"\"{safety.name}\" in Settings, so you can switch back.", request)

    @app.post("/settings/restore/upload")
    async def restore_upload(request: Request):
        upload = (await request.form()).get("file")
        if upload is None or isinstance(upload, str) or not upload.filename:
            return go("/settings#backups", "Pick a backup file first.", request)

        def chunks():
            while True:
                part = upload.file.read(1024 * 1024)
                if not part:
                    return
                yield part

        try:
            saved = await run_in_threadpool(backup.save_upload, cfg, chunks())
        except backup.RestoreError as exc:
            return go("/settings#backups", str(exc), request)
        return go(f"/settings/restore?{urlencode({'file': saved.name})}", None)

    @app.post("/settings")
    async def save_settings(request: Request):
        data = await form(request)
        try:
            values = _settings_values(data)
        except (store.InvalidInput, units.BadTemperature) as exc:
            return go("/settings", str(exc), request)
        with db() as conn:
            put_settings(conn, {k: v for k, v in values.items() if k != "onboarded"})
        return go(back(data.get("back"), "/settings"), "Settings saved.", request)

    @app.post("/settings/backup")
    def backup_now(request: Request):
        result = backup.run_backup(cfg, "manual")
        if result.ok and result.path:
            return go("/settings#backups", f"Backup saved: {result.path.name}", request)
        return go("/settings#backups", f"Backup failed: {result.error}", request)

    @app.get("/settings/database.sqlite3")
    def download_database():
        result = backup.run_backup(cfg, "download")
        if not (result.ok and result.path):
            raise HTTPException(500, "Could not create a backup copy.")
        stream = io.BytesIO(Path(result.path).read_bytes())
        return StreamingResponse(stream, media_type="application/vnd.sqlite3",
                                 headers={"Content-Disposition": f'attachment; filename="{result.path.name}"'})

    # ------------------------------------------------------------ people

    @app.get("/who", response_class=HTMLResponse)
    def who_page(request: Request, next: str = "/"):
        with db() as conn:
            people = profiles.all_profiles(conn)
        if not people:
            return go("/people")
        return render(request, "who.html", active="", people_list=people, nxt=back(next))

    # ------------------------------------------------------------ change history

    HISTORY_PAGE = 60

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request, page: int = 1):
        page = max(page, 1)
        with db() as conn:
            events = history.events(conn, cfg.tz, limit=HISTORY_PAGE + 1, offset=(page - 1) * HISTORY_PAGE)
        return render(request, "history.html", active="settings", events=events[:HISTORY_PAGE], page=page,
                      more=len(events) > HISTORY_PAGE)

    @app.post("/history/{event_id}/put-back")
    async def history_put_back(request: Request, event_id: int):
        data = await form(request)
        try:
            with db() as conn:
                entity, new_id = history.put_back(conn, event_id)
        except history.CannotPutBack as exc:
            return go(back(data.get("back"), "/history"), str(exc), request)
        target = f"/dose/{new_id}" if entity == "dose" else f"/symptoms/{new_id}"
        return go(target, "Put back. It's in the record again.", request)

    @app.post("/who")
    async def who_pick(request: Request):
        data = await form(request)
        with db() as conn:
            row = profiles.get(conn, int(data.get("profile_id") or 0))
        if row is None or not row["active"]:
            return go("/who", "Pick someone from the list.", request)
        request.session["profile_id"] = row["id"]
        return go(back(data.get("next")), {"text": f"Hi {row['name']}. Entries you log now show your name.", "auto": True}, request)

    @app.get("/people", response_class=HTMLResponse)
    def people_page(request: Request):
        with db() as conn:
            active = profiles.all_profiles(conn, active=True)
            archived = profiles.all_profiles(conn, active=False)
            counts = profiles.entry_counts(conn)
        return render(request, "people.html", active="settings", active_people=active, archived=archived,
                      counts=counts)

    @app.post("/people")
    async def people_add(request: Request):
        data = await form(request)
        try:
            with db() as conn:
                pid = profiles.create(conn, data.get("name"), data.get("role"))
        except profiles.InvalidProfile as exc:
            return go("/people", str(exc), request)
        if data.get("use_now") or not request.session.get("profile_id"):
            request.session["profile_id"] = pid
        return go(back(data.get("back"), "/people"), "Added.", request)

    @app.post("/people/{profile_id}")
    async def people_save(request: Request, profile_id: int):
        data = await form(request)
        try:
            with db() as conn:
                profiles.update(conn, profile_id, data.get("name"), data.get("role"))
        except profiles.InvalidProfile as exc:
            return go("/people", str(exc), request)
        return go("/people", "Saved. Past entries show the new name.", request)

    @app.post("/people/{profile_id}/active")
    async def people_active(request: Request, profile_id: int):
        data = await form(request)
        on = data.get("active") == "1"
        with db() as conn:
            if not on and len(profiles.all_profiles(conn)) <= 1:
                return go("/people", "Keep at least one person.", request)
            profiles.set_active(conn, profile_id, on)
        if not on and request.session.get("profile_id") == profile_id:
            request.session.pop("profile_id", None)
        return go("/people", "Back on the list." if on else "Removed from the list. Their past entries keep their name.",
                  request)

    # ------------------------------------------------------------ setup walkthrough

    @app.get("/setup", response_class=HTMLResponse)
    def setup_welcome(request: Request):
        return render(request, "setup_welcome.html", step=0)

    @app.get("/setup/people", response_class=HTMLResponse)
    def setup_people(request: Request):
        with db() as conn:
            existing = profiles.all_profiles(conn)
        return render(request, "setup_people.html", step=1, existing=existing, error=None, values={})

    @app.post("/setup/people")
    async def setup_people_save(request: Request):
        data = await form(request)
        names = _as_list(data.get("names"))
        roles = [str(data.get(f"roles[{i}]") or "family") for i in range(len(names))]
        choice = str(data.get("me") or "")
        with db() as conn:
            existing = profiles.all_profiles(conn)
            created: list[int] = []
            try:
                for i, name in enumerate(names):
                    if store.clean(name):
                        created.append(profiles.create(conn, name, roles[i] if i < len(roles) else "family"))
                    elif i == 0 and not existing:
                        raise profiles.InvalidProfile("Enter your name, so entries show who logged them.")
            except profiles.InvalidProfile as exc:
                conn.rollback()
                return render(request, "setup_people.html", status_code=400, step=1, existing=existing,
                              error=str(exc), values=data)
            ids = [p["id"] for p in existing]
            if choice.startswith("p:") and choice[2:].isdigit() and int(choice[2:]) in ids:
                request.session["profile_id"] = int(choice[2:])
            elif created:
                request.session["profile_id"] = created[0]
            elif ids:
                request.session.setdefault("profile_id", ids[0])
        return go("/setup/cycle")

    @app.get("/setup/medicines", response_class=HTMLResponse)
    def setup_medicines(request: Request):
        with db() as conn:
            chosen = {m["info_key"] for m in store.medications(conn, active=True) if m["info_key"]}
            custom = [m for m in store.medications(conn, active=True) if not m["info_key"]]
            rx = _rx_context(conn, "", now().date())
        return render(request, "setup_medicines.html", step=3, catalog=medinfo.by_category(), chosen=chosen,
                      custom=custom, shortcuts=medinfo.REGIMEN_SHORTCUTS, **rx)

    @app.post("/setup/medicines")
    async def setup_medicines_save(request: Request):
        data = await form(request)
        keys = set(_as_list(data.get("keys")))
        customs = [c for c in (store.clean(x) for x in _as_list(data.get("custom_name"))) if c]
        with db() as conn:
            if data.get("action") == "preview":
                rx = _rx_context(conn, str(data.get("rx_text") or ""), now().date())
                custom = [m for m in store.medications(conn, active=True) if not m["info_key"]]
                chosen = keys | {m.info_key for m in rx["rx_meds"] if m.info_key}
                return render(request, "setup_medicines.html", step=3, catalog=medinfo.by_category(), chosen=chosen,
                              custom=custom, shortcuts=medinfo.REGIMEN_SHORTCUTS, **rx)
            rx_keys = {m.info_key for i, m in _included_rx(data) if m.info_key}
            keys |= rx_keys
            stopped_names = []
            for m in store.medications(conn, active=True):
                if m["info_key"] and m["info_key"] not in keys:
                    store.set_active(conn, m["id"], False)
                    stopped_names.append(m["name"])
            for key in keys - rx_keys:
                store.add_from_catalog(conn, key)
            _import_rx(conn, data)
            for name in customs:
                med_id = store.create_medication(conn, {"name": name, "route": "oral", "category": "custom"})
                store.create_plan(conn, med_id, {"kind": "as_needed", "start_day": "1", "end_day": ""})
            count = len(store.medications(conn, active=True))
        if not count:
            return go("/setup/medicines", "Tick at least one medicine, or add one that isn't listed.", request)
        if stopped_names:
            return go("/setup/label/0", f"Stopped {', '.join(stopped_names)} because {'it was' if len(stopped_names) == 1 else 'they were'} "
                      "unticked. Their history is kept. Start them again from Medicines if that was a mistake.", request)
        return go("/setup/label/0")

    @app.get("/setup/label/{index}", response_class=HTMLResponse)
    def setup_label(request: Request, index: int):
        with db() as conn:
            meds = _setup_order(store.medications(conn, active=True))
            if not meds:
                return go("/setup/medicines")
            if index >= len(meds):
                return go("/setup/team")
            med = meds[index]
            plans = store.plans(conn, med["id"])
        return render(request, "setup_label.html", step=4, med=med, plan=plans[0] if plans else None,
                      extra_plans=len(plans) - 1, index=index, total=len(meds), info=medinfo.lookup(med["info_key"]),
                      error=None, length=14)

    @app.post("/setup/label/{index}")
    async def setup_label_save(request: Request, index: int):
        data = await form(request)
        with db() as conn:
            meds = _setup_order(store.medications(conn, active=True))
            if index >= len(meds):
                return go("/setup/team")
            med = meds[index]
            plans = store.plans(conn, med["id"])
            try:
                store.update_medication(conn, med["id"], data)
                if plans:
                    store.update_plan(conn, plans[0]["id"], data)
                else:
                    store.create_plan(conn, med["id"], data)
            except store.InvalidInput as exc:
                conn.rollback()
                return render(request, "setup_label.html", status_code=400, step=4, med=med,
                              plan=_plan_from_form(data, plans[0] if plans else None), extra_plans=len(plans) - 1,
                              index=index, total=len(meds), info=medinfo.lookup(med["info_key"]), error=str(exc),
                              length=14, values=data)
        return go(f"/setup/label/{index + 1}")

    @app.get("/setup/cycle", response_class=HTMLResponse)
    def setup_cycle(request: Request):
        with db() as conn:
            existing = conn.execute("SELECT * FROM cycles ORDER BY number LIMIT 1").fetchone()
            total = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        values = dict(existing) if existing else {"number": 1, "day1": now().date().isoformat(), "length_days": 14,
                                                  "regimen": "", "count": 4}
        return render(request, "setup_cycle.html", step=2, values=values, existing=existing, total=total, error=None)

    @app.post("/setup/cycle")
    async def setup_cycle_save(request: Request):
        data = await form(request)
        try:
            with db() as conn:
                existing = conn.execute("SELECT * FROM cycles ORDER BY number LIMIT 1").fetchone()
                if existing:
                    store.update_cycle(conn, existing["id"], data)
                else:
                    first = store.validate_cycle(data)
                    count = store.parse_int(str(data.get("count") or "1"), "How many cycles", 1, 60) or 1
                    store.create_cycle_series(conn, {**data, "count": str(count)})
                    if store.clean(data.get("then_count")):
                        then_len = store.parse_int(data.get("then_length_days"), "Days in each later cycle", 1, 90) or 7
                        gap = store.parse_int(data.get("then_gap"), "Days until the next part", 1, 90) or first["length_days"]
                        last_day1 = date.fromisoformat(first["day1"]) + timedelta(days=(count - 1) * first["length_days"])
                        store.create_cycle_series(conn, {
                            "number": str(first["number"] + count), "day1": (last_day1 + timedelta(days=gap)).isoformat(),
                            "length_days": str(then_len), "count": data.get("then_count"),
                            "regimen": data.get("then_regimen") or ""})
        except store.InvalidInput as exc:
            return render(request, "setup_cycle.html", status_code=400, step=2, values=data, existing=None, total=0,
                          error=str(exc))
        return go("/setup/medicines")

    @app.get("/setup/team", response_class=HTMLResponse)
    def setup_team(request: Request):
        with db() as conn:
            total = len(store.medications(conn, active=True))
        return render(request, "setup_team.html", step=5, error=None, last_label=max(total - 1, 0))

    @app.post("/setup/team")
    async def setup_team_save(request: Request):
        data = await form(request)
        try:
            values = _settings_values(data)
        except (store.InvalidInput, units.BadTemperature) as exc:
            return render(request, "setup_team.html", status_code=400, step=5, error=str(exc), last_label=0)
        with db() as conn:
            put_settings(conn, {**values, "onboarded": "1"})
        return go("/", "All set. Today shows what's due. Tap a symptom any time you notice one.", request)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(("/report", "/settings/database")) or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        message = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else "That page doesn't exist."
        return render(request, "error.html", status_code=exc.status_code, message=message)

    return app


# ---------------------------------------------------------------- module helpers


def _settings_values(data: dict) -> dict[str, str]:
    """Form fields -> stored settings. The fever line is typed in the chosen unit and stored in °F."""
    values = {k: str(v).strip() for k, v in data.items() if isinstance(v, str)}
    unit = units.norm(values.get("temp_unit") or "F")
    values["temp_unit"] = unit
    if values.get("fever_line"):
        values["fever_f"] = f"{units.parse_fever_line(values.pop('fever_line'), unit):g}"
    elif values.get("fever_f"):
        values["fever_f"] = f"{units.parse_fever_line(values['fever_f'], 'F'):g}"
    values.pop("fever_line", None)
    return values


def _fever(prefs: dict[str, str]) -> float:
    try:
        return float(prefs.get("fever_f") or 100.4)
    except ValueError:
        return 100.4


def _reference_cycle(conn: sqlite3.Connection, today: date) -> sqlite3.Row | None:
    """The cycle whose Day 1 the prescription preview counts from."""
    found = schedule.cycle_for_date(conn, today) or schedule.next_cycle_after(conn, today)
    if found is None:
        cycles = schedule.all_cycles(conn)
        found = cycles[-1] if cycles else None
    return found


def _cycle_groups(conn: sqlite3.Connection) -> list[tuple[str, int, int]]:
    """Runs of consecutive cycles with the same treatment name, e.g. AC 1-4 then paclitaxel 5-16."""
    groups: list[tuple[str, int, int]] = []
    for c in sorted(schedule.all_cycles(conn), key=lambda c: c["number"]):
        name = c["regimen"] or ""
        if groups and groups[-1][0] == name and c["number"] == groups[-1][2] + 1:
            groups[-1] = (name, groups[-1][1], c["number"])
        else:
            groups.append((name, c["number"], c["number"]))
    return groups


def _rx_context(conn: sqlite3.Connection, text: str, today: date) -> dict:
    cycle = _reference_cycle(conn, today)
    groups = _cycle_groups(conn)
    return {
        "rx_text": text,
        "rx_meds": rxparse.parse(text) if text.strip() else [],
        "rx_cycle": cycle,
        "rx_day1": schedule.day1_of(cycle) if cycle else None,
        "rx_groups": groups if len(groups) > 1 else [],
        "rx_example": RX_EXAMPLE,
        "rx_existing": {m["info_key"] for m in store.medications(conn, active=True) if m["info_key"]},
    }


RX_EXAMPLE = (
    "Ondansetron HCl 8MG TAB NORT - Take one tablet by mouth every eight hours\n"
    "Prochlorperazine Maleate 10 MG TAB NORT - Take one tablet by mouth every six hours\n"
    "dexAMETHasone 4MG TAB NORT - Take 2 tablets by mouth daily. Take on days 2, 3, AND 4, AFTER CHEMO THEN STOP\n"
    "OLANZapine 5MG TAB NORT - TAKE 1 TABLET BY MOUTH AT BEDTIME. Take on days 2, 3 AND 4 AFTER CHEMO THEN STOP"
)


def _included_rx(data: dict) -> list[tuple[int, rxparse.ParsedMed]]:
    text = str(data.get("rx_text") or "")
    if not text.strip():
        return []
    meds = rxparse.parse(text)
    if data.get("rx_seen"):
        include = {str(x) for x in _as_list(data.get("rx_include"))}
        picked = [(i, m) for i, m in enumerate(meds) if str(i) in include]
    else:  # pasted and saved without looking at the preview: take everything that was read
        picked = list(enumerate(meds))
    for i, m in picked:
        name = store.clean(data.get(f"rx_name_{i}"))
        if name and not m.info_key:
            m.name = name
    return [(i, m) for i, m in picked if m.name]


def _import_rx(conn: sqlite3.Connection, data: dict) -> list[int]:
    ids = []
    with atomic(conn):  # every pasted medicine, or none if one of them can't be saved
        for i, m in _included_rx(data):
            rng = str(data.get(f"rx_cycles_{i}") or "all")
            cycles = None
            if re.fullmatch(r"\d+-\d+", rng):
                a, b = (int(x) for x in rng.split("-"))
                cycles = (a, b)
            ids.append(store.import_parsed(conn, m, cycles))
    return ids


def rx_how(m: rxparse.ParsedMed) -> str:
    kinds = {p.kind for p in m.plans}
    if "clinic" in kinds:
        return "Given at the clinic"
    if kinds == {"as_needed"}:
        text = "As needed"
        if m.indication:
            text += f" for {m.indication}"
        if m.min_hours_between:
            text += f", at least {m.min_hours_between:g} hours apart"
        if m.max_per_day:
            text += f", up to {m.max_per_day} a day"
        return text
    times = sorted({t for p in m.plans for t in p.times})
    return "On a schedule" + (f" at {', '.join(schedule.fmt_time(t) for t in times)}" if times else "")


def rx_days(p: rxparse.ParsedPlan, day1: date | None) -> str:
    if p.end_day is None:
        text = "Every day of the cycle" if p.start_day <= 1 else f"Day {p.start_day} to the end of the cycle"
    else:
        text = schedule.days_text(p.start_day, p.end_day)
    if day1 is not None:
        first = day1 + timedelta(days=p.start_day - 1)
        if p.end_day is None:
            text += f" (from {schedule.fmt_day(first)})"
        elif p.end_day == p.start_day:
            text += f" ({schedule.fmt_day(first)})"
        else:
            text += f" ({schedule.fmt_day(first)} – {schedule.fmt_day(day1 + timedelta(days=p.end_day - 1))})"
    return text


# Medicines page, "At home": grouped by type, in the order a family reaches for them. Unknown types go last.
MEDICINE_GROUP_ORDER = ("nausea", "blood", "comfort", "oral_cancer", "premed", "infusion")


def _medicine_groups(meds: list[sqlite3.Row]) -> list[tuple[str, str, str, list[sqlite3.Row]]]:
    info = {key: (name, blurb) for key, name, blurb in medinfo.CATEGORIES}
    out = []
    for key in (*MEDICINE_GROUP_ORDER, "other"):
        group = sorted((m for m in meds if (m["category"] if m["category"] in MEDICINE_GROUP_ORDER else "other") == key),
                       key=lambda m: m["name"].lower())
        if group:
            name, blurb = info.get(key, ("Other medicines", "Added by name"))
            out.append((key, name, blurb, group))
    return out


def _medicine_sections(meds: list[sqlite3.Row], plans: dict[int, list[sqlite3.Row]]):
    """Medicines and the "which medicine?" picker share this order.

    At home is what the family gives, so it's grouped by type. The clinic's medicines are one plain list.
    """
    def clinic_only(m: sqlite3.Row) -> bool:
        return bool(plans[m["id"]]) and all(p["kind"] == "clinic" for p in plans[m["id"]])
    clinic = sorted((m for m in meds if clinic_only(m)), key=lambda m: m["name"].lower())
    return _medicine_groups([m for m in meds if not clinic_only(m)]), clinic


def _chosen_time(choice: str, slot, at_time: str | None, current: datetime) -> datetime:
    """The time picked in "When was it taken?": now, or a clock time on the dose's day.

    "planned" is no longer offered (exact times only) but is still read, so a page left open from before
    the change logs the time it showed instead of falling through to now.
    An evening or bedtime dose logged at an early-morning time was taken after midnight.
    Raises ValueError when "other time" has no usable time.
    """
    if choice == "planned" and slot.time:
        return slot.dt
    if choice == "at":
        hh, mm = (int(x) for x in str(at_time or "").split(":")[:2])
        when = datetime.combine(slot.date, datetime.min.time()).replace(hour=hh, minute=mm)
        # An afternoon, evening or bedtime dose with a morning clock time was given the next morning ("From last
        # night" stays on Today until noon), or after midnight. A later time than now is refused by the caller.
        if slot.time and slot.time >= "12:00" and hh < 12 and (slot.dt - when > timedelta(hours=8) or hh < 5):
            when += timedelta(days=1)
        return when
    return current


def _day_words(when: datetime, current: datetime) -> str:
    """'' for today, ' yesterday', or ' on Tue, Oct 13', for messages about a logged time."""
    if when.date() == current.date():
        return ""
    return " yesterday" if (current.date() - when.date()).days == 1 else f" on {schedule.fmt_day(when.date())}"


def _clinic_note(conn: sqlite3.Connection, med: sqlite3.Row, plans: list[sqlite3.Row], day: date) -> str | None:
    """Why to pause before logging a clinic-only medicine at home, or None when it's fine.

    Shown when every plan is a clinic plan and `day` isn't one of its clinic days this cycle. Usually a mis-tap
    (an IV anti-nausea medicine for the tablet taken at home), sometimes a family recording an infusion later.
    """
    if not plans or any(p["kind"] != "clinic" for p in plans):
        return None
    cycle = schedule.cycle_for_date(conn, day)
    number = schedule.day_number(cycle, day) if cycle else None
    if cycle is not None and any(
        p["medication_id"] == med["id"] and p["kind"] == "clinic" and p["start_day"] <= number <= schedule.plan_end(p, cycle)
        for p in schedule.plans_for(conn, cycle)
    ):
        return None
    days = " and ".join(dict.fromkeys(schedule.days_text(p["start_day"], p["end_day"]).lower() for p in plans))
    note = f"The infusion team gives {med['name']} at the clinic on {days}."
    if cycle is not None and 0 <= number <= int(cycle["length_days"]):
        note += " Today is the day before chemo." if number == 0 else f" Today is day {number}."
    return note


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _setup_order(meds: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Walk the label step in a sensible order: home medicines first, then clinic, then custom."""
    order = {key: i for i, (key, _, _) in enumerate(medinfo.CATEGORIES)}
    priority = {"nausea": 0, "blood": 1, "oral_cancer": 2, "comfort": 3, "premed": 4, "infusion": 5}
    return sorted(meds, key=lambda m: (priority.get(m["category"] or "", 6), order.get(m["category"] or "", 9), m["name"]))


def _plan_from_form(data: dict, existing: sqlite3.Row | None) -> dict:
    out = dict(existing) if existing else {}
    days = _as_list(data.get("days"))
    picked = sorted(int(d) for d in days if str(d).isdigit())
    out.update({
        "kind": data.get("kind") or out.get("kind") or "scheduled",
        "start_day": picked[0] if picked else out.get("start_day", 1),
        "end_day": None if data.get("whole_cycle") else (picked[-1] if picked else out.get("end_day")),
        "times": ",".join(t for t in _as_list(data.get("times")) if t),
        "label": data.get("label") or "",
        "dose": data.get("dose") or "",
    })
    return out


def _focus_med(plan: schedule.DayPlan, nxt: list[schedule.Slot]):
    """The medicine worth explaining in the side panel: the next one due."""
    for s in nxt:
        info = medinfo.lookup(s.plan["med_info"])
        if info:
            return s.plan, info
    for s in plan.scheduled:
        info = medinfo.lookup(s.plan["med_info"])
        if info:
            return s.plan, info
    return None


def _week_summary(rows: list[sqlite3.Row], day: date, fever: float):
    days = [day - timedelta(days=i) for i in range(6, -1, -1)]
    by: dict[str, dict[date, int]] = {}
    for r in rows:
        d = date.fromisoformat(r["logged_at"][:10])
        name = sx.lookup(r["symptom"]).name if r["symptom"] != "other" else (r["label"] or "Other")
        by.setdefault(name, {})
        by[name][d] = max(by[name].get(d, 0), schedule.symptom_score(r, fever))
    return days, by


def ring_geometry(days: list[schedule.RingDay], size: int = 240, compact: bool = False) -> dict:
    """Dot positions for the cycle dial: one dot per day around a ring, Day 1 at 12 o'clock, clockwise.

    Index 0 is Day 0 (the day before chemo), just left of the top. Long cycles get smaller dots so
    neighbours never touch. The full dial leaves room outside the ring for the symptom marks.
    """
    import math

    n = max(len(days), 1)
    c = size / 2
    base = size / (16 if compact else 30)
    pad = 1.7 if compact else 2.6
    dot = min(base, 2 * math.pi * (c - pad * base) / n * 0.38)
    r = c - pad * dot - 1
    out = []
    for i, d in enumerate(days):
        a = math.radians(-90 + (i - 1) * 360 / n)
        cos, sin = math.cos(a), math.sin(a)
        out.append({
            "day": d, "x": round(c + r * cos, 2), "y": round(c + r * sin, 2),
            "sx": round(c + (r + dot * 2.1) * cos, 2), "sy": round(c + (r + dot * 2.1) * sin, 2),
            "lx": round(c + (r - dot * 2.7) * cos, 2), "ly": round(c + (r - dot * 2.7) * sin + size * 0.022, 2),
        })
    return {"dots": out, "size": size, "c": c, "r": round(r, 2), "dot": round(dot, 2),
            "stroke": round(max(1.5, dot * 0.24), 2), "halo": round(dot * 1.45, 2),
            "halo_w": round(max(1.75, dot * 0.26), 2), "sym": round(dot * 0.4, 2),
            "track": round(max(1.25, size / 160), 2)}


def short_wait(an: schedule.AsNeeded, now: datetime) -> str:
    if an.next_allowed is None:
        return ""
    minutes = max(int(an.remaining(now).total_seconds() // 60), 0)  # real time, also the night the clocks change
    return f"{minutes // 60}h" if minutes >= 120 else f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}"


def _template_globals(templates: Jinja2Templates) -> None:
    env = templates.env
    env.filters["fmt_time"] = schedule.fmt_time
    env.filters["person_name"] = lambda pid, people: profiles.name_of(people, pid)
    env.globals.update(
        ring_geometry=ring_geometry,
        local_from_utc=schedule.local_from_utc,
        icon_paths=icons.paths,
        show_temp=units.show,
        rx_how=rx_how,
        rx_days=rx_days,
        initials=profiles.initials,
        roles=profiles.ROLES,
        short_wait=short_wait,
        dose_stamp=store.dose_stamp,
        fmt_hours=schedule.fmt_hours,
        score=schedule.symptom_score,
        fmt_time=schedule.fmt_time,
        fmt_day=schedule.fmt_day,
        fmt_rel=schedule.fmt_relative_day,
        parse_local=schedule.parse_local,
        day_label=schedule.day_label,
        days_text=schedule.days_text,
        plan_times=schedule.plan_times,
        period_names=schedule.PERIOD_NAMES,
        period_of=schedule.period_for,
        reminder_problem=reminders.problem_text,
        kind_names=medinfo.KIND_NAMES,
        route_names=medinfo.ROUTE_NAMES,
        category_names=medinfo.CATEGORY_NAMES,
        medinfo=medinfo,
        sx=sx,
        helped_names=store.HELPED,
        feel_names=store.DOSE_FEEL,
        severity_names=sx.SEVERITY_NAMES,
        lookup_med=medinfo.lookup,
        lookup_symptom=sx.lookup,
        source_note=medinfo.SOURCE_NOTE,
        timedelta=timedelta,
    )
