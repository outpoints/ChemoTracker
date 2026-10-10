"""FastAPI app: server-rendered pages, plain HTML forms, post/redirect/get."""

from __future__ import annotations

import hmac
import io
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, backup, export, medinfo, profiles, schedule, store
from . import symptoms as sx
from .config import PACKAGE_DIR, Settings, load_settings
from .db import get_settings, migrate, put_settings, session

PUBLIC_PATHS = ("/static/", "/healthz", "/login", "/manifest.webmanifest")
# Pages that work before anyone on this device has said who they are.
NO_PROFILE_PATHS = PUBLIC_PATHS + ("/who", "/people", "/setup", "/logout", "/report.xlsx", "/report-",
                                   "/settings/database")


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    cfg = settings or load_settings()
    migrate(cfg.db_path)
    scheduler = backup.BackupScheduler(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="ChemoTracker", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.scheduler = scheduler
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    _template_globals(templates)

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
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
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Added after the gate so it wraps it (Starlette applies middleware outermost-last).
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, same_site="lax", max_age=60 * 60 * 24 * 60,
                       session_cookie="chemotracker")
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    # ------------------------------------------------------------ helpers

    def now() -> datetime:
        return datetime.now(cfg.tz).replace(tzinfo=None, second=0, microsecond=0)

    def db():
        return session(cfg.db_path)

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
                "flash": request.session.pop("flash", None),
                "password_on": bool(cfg.password),
                "version": __version__,
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
        if cfg.password and hmac.compare_digest(str(data.get("password", "")).encode(), cfg.password.encode()):
            request.session["auth"] = True
            return go(back(data.get("next")))
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
            span = schedule.cycle_span(conn, plan.cycle) if plan.cycle else 0
        return render(
            request, "today.html", active="today", plan=plan, ring=ring, upcoming=upcoming, next_doses=nxt,
            follow_ups=follow, feelings=feelings, other_prn=other_prn, focus=focus, span=span, day=day,
            upcoming_day1=schedule.day1_of(upcoming) if upcoming else None,
            quick=[sx.BY_KEY[k] for k in sx.QUICK],
        )

    @app.post("/slot")
    async def record_slot(request: Request):
        data = await form(request)
        current = now()
        with db() as conn:
            plan_row = store.plan(conn, int(data["plan_id"]))
            if plan_row is None:
                raise HTTPException(404)
            slot = schedule.find_slot(conn, plan_row["id"], data["slot"])
            if slot is None:
                raise HTTPException(404, "That dose isn't on the schedule any more.")
            status = data.get("status", "taken")
            # Logging an earlier day's dose after the fact: default to its planned time, not "now".
            if slot.date == current.date():
                default_time = current
            elif slot.time:
                default_time = slot.dt
            else:  # clinic dose on another day: keep it on that day; the report shows no time for clinic doses
                default_time = datetime.combine(slot.date, current.time())
            taken_at = store.parse_local_dt(data.get("taken_at"), default_time)
            med = store.medication(conn, plan_row["medication_id"])
            log_id = store.log_slot(conn, plan_row, slot.key, status, taken_at, slot.dose or None, by=me(request))
        name = med["name"] if med else "Dose"
        msg = (
            {"text": f"{name} marked taken at {schedule.fmt_time(taken_at)}.", "undo": log_id,
             "follow": log_id if plan_row["kind"] != "clinic" else None}
            if status == "taken" else {"text": f"{name} marked skipped.", "undo": log_id}
        )
        return go(back(data.get("back")), msg, request)

    @app.post("/dose/{log_id}/undo")
    async def undo_dose(request: Request, log_id: int):
        data = await form(request)
        with db() as conn:
            store.delete_dose_log(conn, log_id)
        return go(back(data.get("back")), "Undone.", request)

    @app.post("/dose/quick")
    async def quick_dose(request: Request):
        data = await form(request)
        current = now()
        with db() as conn:
            med = store.medication(conn, int(data["medication_id"]))
            if med is None:
                raise HTTPException(404)
            log_id = store.log_dose(conn, med["id"], current, med["dose"], data.get("reason"),
                                    int(data["plan_id"]) if data.get("plan_id") else None, by=me(request))
        return go(back(data.get("back")),
                  {"text": f"{med['name']} logged at {schedule.fmt_time(current)}.", "undo": log_id, "follow": log_id},
                  request)

    @app.get("/dose/new", response_class=HTMLResponse)
    def new_dose(request: Request, med: int, reason: str = ""):
        with db() as conn:
            m = store.medication(conn, med)
            if m is None:
                raise HTTPException(404)
            info = schedule.as_needed_for_symptom(conn, reason, now()) if reason else []
        return render(request, "dose_new.html", active="today", med=m, reason=reason, info=info, error=None,
                      values={"taken_at": now().strftime("%Y-%m-%dT%H:%M"), "dose": m["dose"] or ""})

    @app.post("/dose/new")
    async def create_dose(request: Request):
        data = await form(request)
        current = now()
        with db() as conn:
            m = store.medication(conn, int(data["medication_id"]))
            if m is None:
                raise HTTPException(404)
            try:
                when = store.parse_local_dt(data.get("taken_at"), current)
                log_id = store.log_dose(conn, m["id"], when, data.get("dose"), data.get("reason"), by=me(request))
            except store.InvalidInput as exc:
                return render(request, "dose_new.html", status_code=400, active="today", med=m,
                              reason=data.get("reason", ""), info=[], error=str(exc), values=data)
        return go(f"/dose/{log_id}?saved=1", None)

    @app.get("/dose/{log_id}", response_class=HTMLResponse)
    def dose_page(request: Request, log_id: int, saved: int = 0):
        with db() as conn:
            row = store.dose_log(conn, log_id)
            if row is None:
                raise HTTPException(404)
        return render(request, "dose.html", active="today", log=row, saved=bool(saved), error=None,
                      chips=medinfo.chips_for(row["med_info"]), info=medinfo.lookup(row["med_info"]))

    @app.post("/dose/{log_id}")
    async def save_dose(request: Request, log_id: int):
        data = await form(request)
        with db() as conn:
            row = store.dose_log(conn, log_id)
            if row is None:
                raise HTTPException(404)
            try:
                store.update_dose_log(conn, log_id, data, now(), by=me(request))
            except store.InvalidInput as exc:
                return render(request, "dose.html", status_code=400, active="today", log=row, saved=False,
                              error=str(exc), chips=medinfo.chips_for(row["med_info"]),
                              info=medinfo.lookup(row["med_info"]))
        return go(back(data.get("back")), "Saved.", request)

    @app.post("/dose/{log_id}/follow")
    async def follow_dose(request: Request, log_id: int):
        data = await form(request)
        with db() as conn:
            store.quick_follow_up(conn, log_id, data.get("helped"), data.get("dose_feel"), by=me(request))
        return go(back(data.get("back")), "Thanks, noted.", request)

    @app.post("/dose/{log_id}/delete")
    async def delete_dose(request: Request, log_id: int):
        data = await form(request)
        with db() as conn:
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
        values = dict(entry)
        values["logged_at"] = entry["logged_at"].replace(" ", "T")
        return render(request, "symptom_form.html", active="symptoms", sym=sx.lookup(entry["symptom"]), entry=entry,
                      error=None, values=values, all_symptoms=sx.SYMPTOMS)

    @app.post("/symptoms/{entry_id}")
    async def save_symptom(request: Request, entry_id: int):
        data = await form(request)
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
        return render(request, "medicines.html", active="medicines", meds=meds, plans=plans, available=available)

    @app.get("/medicines/add", response_class=HTMLResponse)
    def add_medicine_page(request: Request):
        with db() as conn:
            taken = {m["info_key"] for m in store.medications(conn, active=True) if m["info_key"]}
        return render(request, "medicine_add.html", active="medicines", taken=taken, chosen=set(), error=None,
                      catalog=medinfo.by_category(), shortcuts=medinfo.REGIMEN_SHORTCUTS)

    @app.post("/medicines/add")
    async def add_medicine(request: Request):
        data = await form(request)
        keys = _as_list(data.get("keys"))
        custom = store.clean(data.get("custom_name"))
        created = []
        with db() as conn:
            for key in keys:
                created.append(store.add_from_catalog(conn, key))
            if custom:
                med_id = store.create_medication(conn, {"name": custom, "route": "oral", "category": "custom"})
                store.create_plan(conn, med_id, {"kind": "as_needed", "start_day": "1", "end_day": ""})
                created.append(med_id)
        if not created:
            return go("/medicines/add", "Tick at least one medicine, or type one that isn't listed.", request)
        if len(created) == 1:
            return go(f"/medicines/{created[0]}?new=1", "Added. Now copy the pharmacy label.", request)
        return go("/medicines", f"Added {len(created)} medicines. Open each one to copy its label.", request)

    @app.get("/medicines/{med_id}", response_class=HTMLResponse)
    def medicine_page(request: Request, med_id: int, new: int = 0):
        with db() as conn:
            med = store.medication(conn, med_id)
            if med is None:
                raise HTTPException(404)
            plans = store.plans(conn, med_id)
            recent = store.recent_doses(conn, med_id, 12)
            cycles = schedule.all_cycles(conn)
            length = max([c["length_days"] for c in cycles] + [14])
        return render(request, "medicine.html", active="medicines", med=med, plans=plans, recent=recent,
                      info=medinfo.lookup(med["info_key"]), is_new=bool(new), error=None, length=length)

    @app.post("/medicines/{med_id}")
    async def save_medicine(request: Request, med_id: int):
        data = await form(request)
        try:
            with db() as conn:
                store.update_medication(conn, med_id, data)
        except store.InvalidInput as exc:
            return go(f"/medicines/{med_id}", str(exc), request)
        return go(back(data.get("back"), f"/medicines/{med_id}"), "Label saved.", request)

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

    @app.post("/medicines/{med_id}/plans")
    async def add_plan(request: Request, med_id: int):
        data = await form(request)
        try:
            with db() as conn:
                store.create_plan(conn, med_id, data)
        except store.InvalidInput as exc:
            return go(f"/medicines/{med_id}#plans", str(exc), request)
        return go(f"/medicines/{med_id}#plans", "Schedule added.", request)

    @app.post("/plans/{plan_id}")
    async def save_plan(request: Request, plan_id: int):
        data = await form(request)
        with db() as conn:
            row = store.plan(conn, plan_id)
            if row is None:
                raise HTTPException(404)
            med_id = row["medication_id"]
            try:
                store.update_plan(conn, plan_id, data)
            except store.InvalidInput as exc:
                return go(back(data.get("back"), f"/medicines/{med_id}#plans"), str(exc), request)
        return go(back(data.get("back"), f"/medicines/{med_id}#plans"), "Schedule saved.", request)

    @app.post("/plans/{plan_id}/delete")
    async def remove_plan(request: Request, plan_id: int):
        with db() as conn:
            row = store.plan(conn, plan_id)
            if row is None:
                raise HTTPException(404)
            store.delete_plan(conn, plan_id)
        return go(f"/medicines/{row['medication_id']}#plans", "Schedule removed.", request)

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
                cycle_id = store.create_cycle(conn, data)
        except store.InvalidInput as exc:
            return go("/cycles", str(exc), request)
        return go(f"/cycles/{cycle_id}", "Cycle added. Medicines follow their schedules from its Day 1.", request)

    @app.get("/cycles/{cycle_id}", response_class=HTMLResponse)
    def cycle_page(request: Request, cycle_id: int):
        current = now()
        with db() as conn:
            c = store.cycle(conn, cycle_id)
            if c is None:
                raise HTTPException(404)
            days, med_rows, sym_rows = schedule.grid(conn, c, current)
            prefs = get_settings(conn)
            ring = schedule.ring(conn, c, current.date(), _fever(prefs))
            cycles = schedule.all_cycles(conn)
        return render(request, "cycle.html", active="cycles", cycle=c, days=days, med_rows=med_rows,
                      sym_rows=sym_rows, ring=ring, cycles=cycles, score=schedule.symptom_score)

    @app.post("/cycles/{cycle_id}")
    async def save_cycle(request: Request, cycle_id: int):
        data = await form(request)
        try:
            with db() as conn:
                store.update_cycle(conn, cycle_id, data)
        except store.InvalidInput as exc:
            return go(f"/cycles/{cycle_id}", str(exc), request)
        return go(f"/cycles/{cycle_id}", "Cycle saved.", request)

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
        with db() as conn:
            cycles = schedule.all_cycles(conn)
            if cycle is None and start is None:
                cur = schedule.cycle_for_date(conn, current.date())
                cycle = str(cur["id"]) if cur else None
            s, e, c = report_range(conn, cycle, start, end)
            prefs = get_settings(conn)
            doses = export.dose_rows(conn, s, e, current)
            syms = export.symptom_rows(conn, s, e, _fever(prefs))
        query = urlencode({"cycle": c["id"]} if c else {"start": s.isoformat(), "end": e.isoformat()})
        return render(request, "report.html", active="report", cycles=cycles, cycle=c, start=s, end=e, doses=doses,
                      syms=syms, query=query)

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
            history = conn.execute("SELECT * FROM backups ORDER BY id DESC LIMIT 8").fetchall()
        files = backup.list_backups(cfg.backup_dir)[:8]
        return render(request, "settings.html", active="settings", history=history, files=files, cfg=cfg,
                      next_backup=scheduler.next_at or backup.next_run(cfg, now()))

    @app.post("/settings")
    async def save_settings(request: Request):
        data = await form(request)
        try:
            store.parse_float(data.get("fever_f"), "Fever line", 97, 104)
        except store.InvalidInput as exc:
            return go("/settings", str(exc), request)
        with db() as conn:
            put_settings(conn, {k: str(v).strip() for k, v in data.items() if k != "onboarded"})
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

    @app.post("/who")
    async def who_pick(request: Request):
        data = await form(request)
        with db() as conn:
            row = profiles.get(conn, int(data.get("profile_id") or 0))
        if row is None or not row["active"]:
            return go("/who", "Pick someone from the list.", request)
        request.session["profile_id"] = row["id"]
        return go(back(data.get("next")), f"Hi {row['name']}. Entries you log now show your name.", request)

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
        return go("/setup/medicines")

    @app.get("/setup/medicines", response_class=HTMLResponse)
    def setup_medicines(request: Request):
        with db() as conn:
            chosen = {m["info_key"] for m in store.medications(conn, active=True) if m["info_key"]}
            custom = [m for m in store.medications(conn, active=True) if not m["info_key"]]
        return render(request, "setup_medicines.html", step=2, catalog=medinfo.by_category(), chosen=chosen,
                      custom=custom, shortcuts=medinfo.REGIMEN_SHORTCUTS)

    @app.post("/setup/medicines")
    async def setup_medicines_save(request: Request):
        data = await form(request)
        keys = set(_as_list(data.get("keys")))
        customs = [c for c in (store.clean(x) for x in _as_list(data.get("custom_name"))) if c]
        with db() as conn:
            for m in store.medications(conn, active=True):
                if m["info_key"] and m["info_key"] not in keys:
                    store.set_active(conn, m["id"], False)
            for key in keys:
                store.add_from_catalog(conn, key)
            for name in customs:
                med_id = store.create_medication(conn, {"name": name, "route": "oral", "category": "custom"})
                store.create_plan(conn, med_id, {"kind": "as_needed", "start_day": "1", "end_day": ""})
            count = len(store.medications(conn, active=True))
        if not count:
            return go("/setup/medicines", "Tick at least one medicine, or add one that isn't listed.", request)
        return go("/setup/label/0")

    @app.get("/setup/label/{index}", response_class=HTMLResponse)
    def setup_label(request: Request, index: int):
        with db() as conn:
            meds = _setup_order(store.medications(conn, active=True))
            if not meds:
                return go("/setup/medicines")
            if index >= len(meds):
                return go("/setup/cycle")
            med = meds[index]
            plans = store.plans(conn, med["id"])
        return render(request, "setup_label.html", step=3, med=med, plan=plans[0] if plans else None,
                      extra_plans=len(plans) - 1, index=index, total=len(meds), info=medinfo.lookup(med["info_key"]),
                      error=None, length=14)

    @app.post("/setup/label/{index}")
    async def setup_label_save(request: Request, index: int):
        data = await form(request)
        with db() as conn:
            meds = _setup_order(store.medications(conn, active=True))
            if index >= len(meds):
                return go("/setup/cycle")
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
                return render(request, "setup_label.html", status_code=400, step=3, med=med,
                              plan=_plan_from_form(data, plans[0] if plans else None), extra_plans=len(plans) - 1,
                              index=index, total=len(meds), info=medinfo.lookup(med["info_key"]), error=str(exc),
                              length=14, values=data)
        return go(f"/setup/label/{index + 1}")

    @app.get("/setup/cycle", response_class=HTMLResponse)
    def setup_cycle(request: Request):
        with db() as conn:
            existing = conn.execute("SELECT * FROM cycles ORDER BY number LIMIT 1").fetchone()
            count = len(store.medications(conn, active=True))
        values = dict(existing) if existing else {"number": 1, "day1": now().date().isoformat(), "length_days": 14,
                                                  "regimen": ""}
        return render(request, "setup_cycle.html", step=4, values=values, existing=existing, error=None, count=count)

    @app.post("/setup/cycle")
    async def setup_cycle_save(request: Request):
        data = await form(request)
        try:
            with db() as conn:
                existing = conn.execute("SELECT * FROM cycles ORDER BY number LIMIT 1").fetchone()
                if existing:
                    store.update_cycle(conn, existing["id"], data)
                else:
                    store.create_cycle(conn, data)
        except store.InvalidInput as exc:
            return render(request, "setup_cycle.html", status_code=400, step=4, values=data, existing=None,
                          error=str(exc), count=0)
        return go("/setup/team")

    @app.get("/setup/team", response_class=HTMLResponse)
    def setup_team(request: Request):
        return render(request, "setup_team.html", step=5, error=None)

    @app.post("/setup/team")
    async def setup_team_save(request: Request):
        data = await form(request)
        try:
            store.parse_float(data.get("fever_f"), "Fever line", 97, 104)
        except store.InvalidInput as exc:
            return render(request, "setup_team.html", status_code=400, step=5, error=str(exc))
        with db() as conn:
            put_settings(conn, {**{k: str(v).strip() for k, v in data.items()}, "onboarded": "1"})
        return go("/", "All set. Today shows what's due. Tap a symptom any time you notice one.", request)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(("/report", "/settings/database")) or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        message = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else "That page doesn't exist."
        return render(request, "error.html", status_code=exc.status_code, message=message)

    return app


# ---------------------------------------------------------------- module helpers


def _fever(prefs: dict[str, str]) -> float:
    try:
        return float(prefs.get("fever_f") or 100.4)
    except ValueError:
        return 100.4


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


def ring_points(days: list[schedule.RingDay], size: int = 240):
    """(day, x, y, outer_x, outer_y) for each cycle day, Day 1 at 12 o'clock, clockwise."""
    import math

    n = max(len(days), 1)
    c = size / 2
    r = size / 2 - 22
    out = []
    for i, d in enumerate(days):
        a = math.radians(-90 + (i - 1) * 360 / n)
        out.append((d, round(c + r * math.cos(a), 2), round(c + r * math.sin(a), 2),
                    round(c + (r + 14) * math.cos(a), 2), round(c + (r + 14) * math.sin(a), 2)))
    return out


def short_wait(an: schedule.AsNeeded, now: datetime) -> str:
    nxt = an.next_allowed
    if nxt is None:
        return ""
    minutes = max(int((nxt - now).total_seconds() // 60), 0)
    return f"{minutes // 60}h" if minutes >= 120 else f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}"


def _template_globals(templates: Jinja2Templates) -> None:
    env = templates.env
    env.filters["fmt_time"] = schedule.fmt_time
    env.filters["person_name"] = lambda pid, people: profiles.name_of(people, pid)
    env.globals.update(
        ring_points=ring_points,
        initials=profiles.initials,
        roles=profiles.ROLES,
        short_wait=short_wait,
        score=schedule.symptom_score,
        fmt_time=schedule.fmt_time,
        fmt_day=schedule.fmt_day,
        fmt_rel=schedule.fmt_relative_day,
        parse_local=schedule.parse_local,
        day_label=schedule.day_label,
        days_text=schedule.days_text,
        plan_times=schedule.plan_times,
        period_names=schedule.PERIOD_NAMES,
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
