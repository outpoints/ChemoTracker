"""Forms posted the way a browser posts them.

Most tests post hand-built data with one value per field. A browser sends every field in the form, in
page order. The setup label page once held two fields named "dose" (the medicine's dose and its
schedule's "Dose for this schedule"). They reached the store as a list, and the "check each label" step
of the first-run walkthrough failed with a server error for every medicine.
"""

from __future__ import annotations

import collections
import html.parser
import re

from chemotracker import store
from chemotracker.db import session

from .conftest import setup_walkthrough, today

SKIP_TYPES = {"submit", "button", "image", "reset", "file"}


class _Forms(html.parser.HTMLParser):
    """Every form on a page as (action, [(name, type, value, checked)]), like a browser builds it."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[tuple[str, list[list]]] = []
        self._fields: list[list] | None = None
        self._template = 0
        self._textarea: list | None = None
        self._select: list | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "template":
            self._template += 1
        if self._template:
            return
        if tag == "form":
            self._fields = []
            self.forms.append((a.get("action") or "", self._fields))
        elif self._fields is None or "disabled" in a:
            return
        elif tag == "input" and a.get("name") and (a.get("type") or "text") not in SKIP_TYPES:
            kind = a.get("type") or "text"
            default = "on" if kind in ("checkbox", "radio") else ""
            self._fields.append([a["name"], kind, a.get("value", default), "checked" in a])
        elif tag == "textarea" and a.get("name"):
            self._textarea = [a["name"], "textarea", "", True]
            self._fields.append(self._textarea)
        elif tag == "select" and a.get("name"):
            self._select = [a["name"], "select", None, True]
            self._fields.append(self._select)
        elif tag == "option" and self._select is not None:
            if self._select[2] is None or "selected" in a:
                self._select[2] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "template":
            self._template -= 1
        elif tag == "form":
            self._fields = None
        elif tag == "textarea":
            self._textarea = None
        elif tag == "select":
            self._select = None

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea[2] += data


def forms(page: str) -> list[tuple[str, list[list]]]:
    parser = _Forms()
    parser.feed(page)
    return parser.forms


def browser_post_data(page: str, action: str, **changes: str) -> dict[str, list[str]]:
    """What the browser sends for the form posting to `action`, after typing `changes` into its text fields."""
    found = [fields for act, fields in forms(page) if act == action]
    assert found, f"no form posts to {action}"
    out: dict[str, list[str]] = collections.defaultdict(list)
    for name, kind, value, checked in found[0]:
        if kind in ("checkbox", "radio") and not checked:
            continue
        out[name].append(changes.get(name, value or ""))
    return dict(out)


def _single_value_names(fields: list[list]) -> list[str]:
    counts = collections.Counter(name for name, kind, _, _ in fields if kind not in ("checkbox", "radio"))
    return sorted(n for n, c in counts.items() if c > 1 and not n.endswith("[]"))


def test_no_form_sends_two_values_for_a_one_value_field(client, cfg):
    """Two fields with one name arrive as a list, which the store can't read as text."""
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        meds = [m["id"] for m in store.medications(conn)]
        cycles = [c["id"] for c in conn.execute("SELECT id FROM cycles")]
    pages = ["/", "/dose/new", "/symptoms", "/symptoms/new?s=nausea", "/medicines", "/medicines/add", "/cycles",
             "/report", "/settings", "/settings/restore", "/who", "/history", "/people", "/setup", "/setup/people",
             "/setup/cycle", "/setup/medicines", "/setup/team"]
    pages += [f"/medicines/{m}" for m in meds] + [f"/cycles/{c}" for c in cycles]
    pages += [f"/setup/label/{i}" for i in range(len(meds))]
    checked = 0
    for path in pages:
        r = client.get(path)
        assert r.status_code == 200, path
        for action, fields in forms(r.text):
            assert _single_value_names(fields) == [], f"{path} form {action}"
            checked += 1
    assert checked > 30  # the parser found the forms


def test_setup_label_step_saves_what_the_browser_sends(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        med = next(m for m in store.medications(conn) if m["name"] == "Olanzapine")
    index = next(i for i in range(10) if f'<h1 style="margin-top: var(--s1)">{med["name"]}' in client.get(f"/setup/label/{i}").text)
    page = client.get(f"/setup/label/{index}").text

    data = browser_post_data(page, f"/setup/label/{index}", dose="1 tablet", plan_dose="2 tablets")
    assert data["dose"] == ["1 tablet"] and data["plan_dose"] == ["2 tablets"]  # two fields, two names
    r = client.post(f"/setup/label/{index}", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]

    with session(cfg.db_path) as conn:
        assert store.medication(conn, med["id"])["dose"] == "1 tablet"
        plan = store.plans(conn, med["id"])[0]
    assert plan["dose"] == "2 tablets"
    assert (plan["kind"], plan["start_day"], plan["end_day"], plan["times"]) == ("scheduled", 2, 4, "21:00")


def test_setup_label_step_keeps_a_schedule_limited_to_some_cycles(client, cfg):
    """The label step has no cycle fields, so saving it must not widen a schedule to every cycle."""
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        med = next(m for m in store.medications(conn) if m["name"] == "Paclitaxel")
        plan = store.plans(conn, med["id"])[0]
        conn.execute("UPDATE plans SET from_cycle = 5, to_cycle = 16 WHERE id = ?", (plan["id"],))
    index = next(i for i in range(10) if f'<h1 style="margin-top: var(--s1)">{med["name"]}' in client.get(f"/setup/label/{i}").text)
    page = client.get(f"/setup/label/{index}").text
    assert not re.search(r'name="(from|to)_cycle"', page)

    r = client.post(f"/setup/label/{index}", data=browser_post_data(page, f"/setup/label/{index}"),
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    with session(cfg.db_path) as conn:
        plan = store.plans(conn, med["id"])[0]
    assert (plan["from_cycle"], plan["to_cycle"]) == (5, 16)
