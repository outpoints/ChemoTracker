"""Today's small scripts in a real browser (Chromium through Playwright). Skipped where Playwright isn't installed.

    uv run --with playwright python -m pytest tests/browser -p no:warnings -o addopts=''

The server runs this checkout on 127.0.0.1 with its clock frozen (web.now), and the page gets Playwright's fake
clock. Both are moved by the same amounts, so "the page was left open for two hours" is exact. The phone is set
to another time zone than the server on purpose: the scripts must work in the server's time.
- S1: the "Other time" clock in every take panel (and on Log a dose) follows the real time until someone changes it.
- S2: an as-needed countdown that reaches zero asks the server again instead of saying "OK to take now" by itself.
- S5: the countdown's labels round down, like the server draws them.
"""

from __future__ import annotations

import html
import socket
import threading
import time as wall
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from chemotracker import schedule, store, web  # noqa: E402
from chemotracker.db import session  # noqa: E402
from chemotracker.web import create_app  # noqa: E402

from ..conftest import setup_walkthrough  # noqa: E402

LA = ZoneInfo("America/Los_Angeles")
PHONE_TZ = "Asia/Tokyo"  # not the server's zone


def _free_port() -> int:
    for port in range(8790, 8800):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 8790-8799")


@pytest.fixture()
def live(cfg, monkeypatch):
    """This checkout on 127.0.0.1, first-run walkthrough done, today is Day 4 and the server's clock says 1:00 PM."""
    start = datetime.combine(datetime.now(LA).date(), time(13, 0))
    state = SimpleNamespace(local=start)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return state.local.replace(tzinfo=LA).astimezone(tz) if tz is not None else state.local

    monkeypatch.setattr(web, "datetime", Frozen)
    with TestClient(create_app(cfg, start_scheduler=False)) as c:
        setup_walkthrough(c, start.date() - timedelta(days=3))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(cfg, start_scheduler=False), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        wall.sleep(0.05)
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{port}", start=start, state=state, cfg=cfg)
    finally:
        server.should_exit = True
        thread.join(5)


@pytest.fixture()
def browser():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def phone(browser, live):
    """John's phone, on Today, with a fake clock that starts at the server's time."""
    ctx = browser.new_context(viewport={"width": 393, "height": 852}, timezone_id=PHONE_TZ, reduced_motion="reduce")
    page = ctx.new_page()
    live.errors = []
    page.on("pageerror", lambda e: live.errors.append(str(e)))
    page.clock.install(time=live.state.local.replace(tzinfo=LA))
    page.goto(live.base + "/who?next=/")
    page.locator("button.who-btn", has_text="John").click()
    page.wait_for_url(live.base + "/")
    return page


def later(live, page, minutes: int) -> None:
    """Time passes with the page open: on the server and on the phone."""
    live.state.local += timedelta(minutes=minutes)
    page.clock.run_for(minutes * 60 * 1000)


def took_one(live, name: str, hhmm: str, who: str = "John") -> None:
    """A "Took one" from the as-needed shelf at a clock time today (as another phone or earlier would have)."""
    with session(live.cfg.db_path, write=True) as conn:
        med = conn.execute("SELECT * FROM medications WHERE name = ?", (name,)).fetchone()
        plan = next(p for p in store.plans(conn, med["id"]) if p["kind"] == "as_needed")
        person = conn.execute("SELECT id FROM profiles WHERE name = ?", (who,)).fetchone()["id"]
        hh, mm = (int(x) for x in hhmm.split(":"))
        store.log_dose(conn, med["id"], datetime.combine(live.start.date(), time(hh, mm)), med["dose"], None, plan["id"],
                       by=person)


def reloaded(page, seconds: float = 6) -> bool:
    """Whether the page was drawn again (the marker set on the old page is gone)."""
    end = wall.monotonic() + seconds
    while wall.monotonic() < end:
        try:
            if page.evaluate("() => window.__drawn === undefined && document.readyState === 'complete'"):
                return True
        except sync_api.Error:
            pass  # navigating
        wall.sleep(0.1)
    return False


def prn(page, name: str):
    return page.locator(".prn-row", has_text=name)


# ============================================================================ S1


def test_other_time_clock_follows_the_real_time_on_a_page_left_open(live, browser):
    """S1. The "Other time" clock was filled in when the page was drawn. Left open for two hours, it still said
    1:00 PM, and accepting it back-dated the dose by two hours, so the next one showed as OK two hours early."""
    page = phone(browser, live)
    row = prn(page, "Prochlorperazine")
    clock = row.locator("input[type=time]")
    assert clock.input_value() == "13:00"
    later(live, page, 120)
    row.locator("summary").click()
    expect(clock).to_have_value("15:00", timeout=3000)  # "13:00" before: the time the page was drawn
    row.get_by_role("button", name="Log it").click()
    page.wait_for_load_state()
    with session(live.cfg.db_path) as conn:
        taken = conn.execute("SELECT taken_at FROM dose_logs ORDER BY id DESC LIMIT 1").fetchone()["taken_at"]
    assert schedule.parse_local(taken) == live.start + timedelta(hours=2), f"logged at {taken}"

    # A time someone typed stays, also after closing and opening the panel again.
    dex = page.locator(".dose", has_text="Dexamethasone")  # 8:00 AM, late by now
    dex.locator("details[data-take] > summary").click()
    dex.locator("input[type=time]").fill("14:10")
    dex.locator("details[data-take] > summary").click()
    later(live, page, 25)
    dex.locator("details[data-take] > summary").click()
    settle()
    assert dex.locator("input[type=time]").input_value() == "14:10"
    # Taken on an untouched row follows the time too.
    fil = page.locator(".dose", has_text="Filgrastim")
    fil.locator("details[data-take] > summary").click()
    expect(fil.locator("input[type=time]")).to_have_value("15:25", timeout=3000)

    # An earlier day keeps its planned time: it isn't "now".
    yesterday = (live.start - timedelta(days=1)).date().isoformat()
    page.goto(f"{live.base}/?d={yesterday}")
    dex = page.locator(".dose", has_text="Dexamethasone")
    assert dex.locator("input[type=time]").input_value() == "08:00"
    later(live, page, 15)
    dex.locator("details[data-take] > summary").click()
    settle()
    assert dex.locator("input[type=time]").input_value() == "08:00"

    # Log a dose: choosing Other time sets an untouched clock to now.
    page.goto(live.base + "/dose/new")
    assert page.locator("#pick_time").input_value() == "15:40"
    later(live, page, 45)
    page.locator(".when-chips label", has_text="Other time").click()
    expect(page.locator("#pick_time")).to_have_value("16:25", timeout=3000)

    # The server still refuses a time that hasn't happened yet.
    page.goto(live.base + "/")
    row = prn(page, "Ondansetron")
    row.locator("summary").click()
    row.locator("input[type=time]").fill("17:15")
    row.get_by_role("button", name="Log it").click()
    page.wait_for_load_state()
    assert "That time hasn't happened yet" in html.unescape(page.content())
    assert live.errors == []


def settle() -> None:
    """Let the page handle the panel it just opened (the toggle event comes a moment after the click)."""
    wall.sleep(0.5)


# ============================================================================ S2


def test_countdown_at_zero_asks_the_server_instead_of_saying_ok(live, browser):
    """S2. John's ring counted down and flipped to "OK to take now" by itself, although Jane had logged one on her
    phone meanwhile. At zero it must draw the page again, which shows the server's answer."""
    took_one(live, "Prochlorperazine", "07:01")  # 6 hours apart: OK at 1:01 PM, a minute from now
    page = phone(browser, live)
    row = prn(page, "Prochlorperazine")
    assert row.locator("[data-status]").inner_text() == "OK at 1:01 PM"
    took_one(live, "Prochlorperazine", "13:00", who="Jane")  # Jane, on her phone
    page.evaluate("() => { window.__drawn = 1; }")
    live.state.local += timedelta(minutes=2)
    page.clock.run_for(90 * 1000)
    status = "" if reloaded(page) else row.locator("[data-status]").inner_text()
    assert reloaded(page, 0.5), f"the page wasn't drawn again; the ring says {status!r} after Jane's dose"
    assert prn(page, "Prochlorperazine").locator("[data-status]").inner_text() == "OK at 7:00 PM"


def test_countdown_at_zero_waits_while_someone_is_logging(live, browser):
    """S2. With a panel open the page isn't drawn again under someone's finger. The ring says "Checking…", never
    "OK to take now", until the panel is closed. Then the page is drawn again with the server's answer."""
    took_one(live, "Prochlorperazine", "07:01")
    page = phone(browser, live)
    other = prn(page, "Ondansetron")
    other.locator("summary").click()  # someone is logging Ondansetron
    page.evaluate("() => { window.__drawn = 1; }")
    live.state.local += timedelta(minutes=2)
    page.clock.run_for(90 * 1000)
    wall.sleep(1)
    row = prn(page, "Prochlorperazine")
    assert row.locator("[data-status]").inner_text() == "Checking…"
    assert "ok" not in (row.locator("svg.countdown").get_attribute("class") or "").split()
    assert page.evaluate("() => window.__drawn") == 1, "the page was drawn again with a panel open"
    other.locator("summary").click()  # Cancel
    page.clock.run_for(20 * 1000)
    assert reloaded(page)
    assert prn(page, "Prochlorperazine").locator("[data-status]").inner_text() == "OK to take now"


# ============================================================================ S5


def short_wait(seconds: float) -> str:
    """The server's ring label for this much time left."""
    left = timedelta(seconds=seconds)
    return web.short_wait(SimpleNamespace(next_allowed=True, remaining=lambda now: left), None)  # type: ignore[arg-type]


def test_countdown_labels_round_down_like_the_server(live, browser):
    """S5. The server rounds the minutes left down, and the script rounded them up, so a label could go up a
    minute on the first tick ("59m", then "1h00"). After a tick they must read as the server would draw them."""
    took_one(live, "Prochlorperazine", "08:30")  # OK at 2:30 PM: 90 minutes left
    took_one(live, "Ondansetron", "05:01")  # 8 hours apart: OK at 1:01 PM, 1 minute left
    page = phone(browser, live)
    pro, ond = prn(page, "Prochlorperazine"), prn(page, "Ondansetron")
    assert pro.locator("[data-left]").text_content() == short_wait(5400) == "1h30"
    assert ond.locator("[data-left]").text_content() == short_wait(60) == "1m"
    page.clock.run_for(20 * 1000)  # the first tick, 20 seconds in
    assert pro.locator("[data-left]").text_content() == short_wait(5400 - 20) == "1h29"
    assert pro.locator("[data-wait]").inner_text() == "in " + schedule.fmt_duration(timedelta(seconds=5400 - 20))
    assert ond.locator("[data-left]").text_content() == short_wait(40) == "0m"
    assert ond.locator("[data-wait]").inner_text() == "in less than a minute"
