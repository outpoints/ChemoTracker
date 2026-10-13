"""The full-size cycle dial names the date of the day it shows, like "Tue · 10/13/26"."""

from __future__ import annotations

import re
from datetime import timedelta

from chemotracker.db import session

from .conftest import setup_walkthrough, today

DATE_LINE = re.compile(r'class="dial-sub dial-date"[^>]*>([^<]+)<')


def _dial_date(d) -> str:
    return f"{d:%a} · {d.month}/{d.day}/{d:%y}"


def test_dial_names_the_date_of_the_day_shown(client, cfg):
    now = today(cfg)
    setup_walkthrough(client, now - timedelta(days=2))

    # Today: only the full-size dial (desktop card) carries the date. The small phone dial stays a number.
    page = client.get("/").text
    assert DATE_LINE.findall(page) == [_dial_date(now)]
    assert f"day 3 of 14, {now:%a}" in page  # read out with the day's date too

    # Browsing to another day dates the ringed day, never today.
    later = now + timedelta(days=2)
    assert DATE_LINE.findall(client.get(f"/?d={later.isoformat()}").text) == [_dial_date(later)]

    # The Cycle page's dial uses the same ring.
    with session(cfg.db_path) as conn:
        cycle_id = conn.execute("SELECT id FROM cycles ORDER BY number LIMIT 1").fetchone()["id"]
    assert DATE_LINE.findall(client.get(f"/cycles/{cycle_id}").text) == [_dial_date(now)]
