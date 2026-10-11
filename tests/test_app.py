from __future__ import annotations

import io
import re
from datetime import date, timedelta

import openpyxl

from chemotracker import backup, profiles, schedule, store
from chemotracker.db import get_settings
from chemotracker.db import session

from .conftest import local_now, setup_walkthrough, today


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_walkthrough_builds_todays_compartments(client, cfg):
    # Make "today" Day 3, the sheet's busiest day.
    day1 = today(cfg) - timedelta(days=2)
    setup_walkthrough(client, day1)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Day 3<span class=\"of\"> of 14</span>" in html and "Cycle 1" in html
    for period in ("Morning", "Afternoon", "Bedtime"):
        assert f'<span class="c-name">{period}</span>' in html
    assert "Dexamethasone" in html and "2 tablets (4 mg)" in html
    assert "Filgrastim" in html and "Olanzapine" in html
    assert "As needed" in html and "Prochlorperazine" in html
    # Ondansetron is planned for Days 4-6 only.
    assert "Not on today&#39;s schedule" in html or "Other as-needed medicines" in html
    assert "How are you feeling?" in html


def test_bedtime_label_lands_in_bedtime(conn):
    assert schedule.period_for("07:15") == "morning"
    assert schedule.period_for("14:00") == "afternoon"
    assert schedule.period_for("18:00") == "evening"
    assert schedule.period_for("21:00") == "bedtime"
    assert schedule.period_for("02:00") == "bedtime"
    assert schedule.period_for("19:00", "At bedtime") == "bedtime"


def test_taking_a_dose_is_idempotent_and_undoable(client, cfg):
    day1 = today(cfg) - timedelta(days=2)
    setup_walkthrough(client, day1)
    with session(cfg.db_path) as conn:
        dex = next(s for s in schedule.slots(conn, schedule.cycle_for_date(conn, today(cfg)), 3)
                   if s.plan["med_name"] == "Dexamethasone")
    for _ in range(2):  # double tap
        r = client.post("/slot", data={"plan_id": dex.plan["id"], "slot": dex.key, "status": "taken", "back": "/"},
                        follow_redirects=False)
        assert r.status_code == 303
    with session(cfg.db_path) as conn:
        rows = conn.execute("SELECT * FROM dose_logs WHERE plan_id = ?", (dex.plan["id"],)).fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "taken" and rows[0]["dose"] == "2 tablets (4 mg)"
        log_id = rows[0]["id"]
    page = client.get("/")
    assert "Dexamethasone marked taken" in page.text and f"/dose/{log_id}/undo" in page.text
    client.post(f"/dose/{log_id}/undo", data={"back": "/"})
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs").fetchone()[0] == 0


def test_as_needed_gap_and_follow_up(client, cfg):
    day1 = today(cfg) - timedelta(days=2)
    setup_walkthrough(client, day1)
    with session(cfg.db_path) as conn:
        compazine = conn.execute("SELECT * FROM medications WHERE name = 'Prochlorperazine'").fetchone()
        assert compazine["min_hours_between"] == 6
        two_hours_ago = local_now(cfg) - timedelta(hours=2)
        log_id = store.log_dose(conn, compazine["id"], two_hours_ago, "1 tablet", "nausea")
        an = schedule._as_needed_row(conn, compazine, None, local_now(cfg))
        assert not an.available(local_now(cfg))
        assert an.next_allowed == two_hours_ago + timedelta(hours=6)
        assert 0.3 < an.fraction(local_now(cfg)) < 0.37
    html = client.get("/").text
    assert "OK at" in html or "OK tomorrow at" in html
    assert "How did the Prochlorperazine" in html  # follow-up prompt after 30 min
    r = client.post(f"/dose/{log_id}/follow", data={"helped": "some", "dose_feel": "not_enough", "back": "/"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        row = store.dose_log(conn, log_id)
        assert row["helped"] == "some" and row["dose_feel"] == "not_enough"


def test_follow_ups_fold_away_under_the_as_needed_shelf(client, cfg):
    # Optional questions must not push due doses down: one closed section below the as-needed shelf,
    # never loose cards above the compartments. Saving one reopens it while others are still waiting.
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    with session(cfg.db_path, write=True) as conn:
        med = conn.execute("SELECT id FROM medications WHERE name = 'Prochlorperazine'").fetchone()["id"]
        first = store.log_dose(conn, med, local_now(cfg) - timedelta(hours=9), "1 tablet", None)
        store.log_dose(conn, med, local_now(cfg) - timedelta(hours=2), "1 tablet", None)
    html = client.get("/").text
    assert 'class="card follow"' not in html
    section = html.split('id="c-ask"')[0].rsplit("<details", 1)[1] + html.split('id="c-ask"')[1].split("</details>")[0]
    assert "Any side effects from your medicine?" in section and "Optional · 2 recent doses" in section
    assert " open>" not in section.split("<summary>")[0]  # closed until someone opens it
    assert section.count('<form class="follow"') == 2 and 'value="/?ask=1#c-ask"' in section
    assert html.index('id="c-prn"') < html.index('id="c-ask"')  # below the shelf it asks about
    client.post(f"/dose/{first}/follow", data={"helped": "yes", "back": "/?ask=1#c-ask"}, follow_redirects=False)
    again = client.get("/?ask=1").text
    head = again.split('id="c-ask"')[1].split("<summary>")[0]
    assert "open" in head and "Optional · 1 recent dose<" in again

def test_as_needed_without_a_wait_time_never_says_ok(client, cfg):
    # With no hours-between saved, the app can't know when the next one is OK, so it must not say so.
    setup_walkthrough(client, today(cfg) - timedelta(days=4))  # Day 5: prochlorperazine is planned
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT * FROM medications WHERE name = 'Prochlorperazine'").fetchone()
        conn.execute("UPDATE medications SET min_hours_between = NULL WHERE id = ?", (med["id"],))
        med = conn.execute("SELECT * FROM medications WHERE id = ?", (med["id"],)).fetchone()
        an = schedule._as_needed_row(conn, med, None, local_now(cfg))
        assert an.state(local_now(cfg)) == "open" and an.fraction(local_now(cfg)) == 0.0
        assert an.status_text(local_now(cfg)) == "Time between doses not saved"
    rows = client.get("/").text.split('<div class="prn-row ')
    row = next(r for r in rows if ">Prochlorperazine</a>" in r).split("</form>")[0]  # just this row, not the rest of the page
    assert row.startswith("open") and "Time between doses not saved" in row and "OK to take now" not in row
    assert "countdown open" in row and "btn-primary" not in row
    assert f"/medicines/{med['id']}?edit=1#gap" in row


def test_messages_can_be_dismissed(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        pid = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()["id"]
    page = client.post("/who", data={"profile_id": str(pid), "next": "/"}).text
    toast = page.split('<div class="toast"')[1].split("</div>")[0]
    assert "Entries you log now show your name" in toast and "data-toast-auto" in toast
    # The X is a real link back to the same page, so it works even with scripts off.
    assert 'class="toast-close" href="/" data-toast-close aria-label="Dismiss this message"' in toast
    assert '<div class="toast"' not in client.get("/").text  # shown once


def test_reminder_name_stays_the_same_between_visits(client, cfg):
    # People leave the page to install ntfy. If the name changed on reload, reminders would never arrive.
    setup_walkthrough(client, today(cfg))
    first = re.search(r'id="topic-name">([^<]+)<', client.get("/settings").text).group(1)
    again = re.search(r'id="topic-name">([^<]+)<', client.get("/settings").text).group(1)
    assert first == again and first.startswith("chemotracker-")


def test_symptom_logging_links_to_rescue_meds_and_flags_fever(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=4))  # Day 5: ondansetron + prochlorperazine allowed
    r = client.post("/symptoms/new", data={"symptom": "nausea", "severity": "2", "notes": "after lunch"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = client.get(r.headers["location"]).text
    assert "Nausea · Moderate" in saved
    assert "Your as-needed medicines for nausea" in saved
    assert "Ondansetron" in saved and "Prochlorperazine" in saved
    assert "callout-urgent" not in saved

    r = client.post("/symptoms/new", data={"symptom": "temperature", "value": "100.8"}, follow_redirects=False)
    saved = client.get(r.headers["location"]).text
    assert "callout-urgent" in saved and "555-0100" in saved

    r = client.post("/symptoms/new", data={"symptom": "nausea"}, follow_redirects=False)
    assert r.status_code == 400 and "Choose how bad" in r.text

    page = client.get("/symptoms").text
    assert "Nausea · Moderate" in page and "100.8\u00a0°F" in page
    assert client.get("/").text.count("sev-tag") >= 2


def test_symptom_suggestions_respect_planned_days(client, cfg):
    # Day 3: ondansetron is only planned for Days 4-6, so it must not say "OK to take now".
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    r = client.post("/symptoms/new", data={"symptom": "nausea", "severity": "2"}, follow_redirects=False)
    saved = client.get(r.headers["location"]).text
    ond = saved.split("Ondansetron")[1].split("prn-row")[0]
    assert "Not planned today (Days 4–6)" in ond and "OK to take now" not in ond and "Took one</span>" not in ond
    comp = saved.split("Prochlorperazine")[1]
    assert "OK to take now" in comp and "Took one</span>" in comp


def test_medicine_and_plan_are_editable(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        olz = conn.execute("SELECT * FROM medications WHERE name = 'Olanzapine'").fetchone()
        plan = store.plans(conn, olz["id"])[0]
    r = client.post(f"/medicines/{olz['id']}", data={"name": "Olanzapine", "strength": "7.5 mg tablet", "dose": "1 tablet",
                                                    "directions": "Take 1 tablet at bedtime", "route": "oral"},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/plans/{plan['id']}", data={"kind": "scheduled", "days[]": ["1", "2", "3", "4"], "times[]": ["20:30"]},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/plans/{plan['id']}", data={"kind": "scheduled", "days[]": ["2", "4"], "times[]": ["20:30"]},
                    follow_redirects=True)
    assert "next to each other" in r.text
    with session(cfg.db_path) as conn:
        assert store.medication(conn, olz["id"])["strength"] == "7.5 mg tablet"
        p = store.plan(conn, plan["id"])
        assert (p["start_day"], p["end_day"], p["times"]) == (1, 4, "20:30")
    page = client.get(f"/medicines/{olz['id']}").text
    assert "About Olanzapine" in page and "MedlinePlus" in page


def test_medicines_are_grouped_by_place_then_type(client, cfg):
    setup_walkthrough(client, today(cfg))
    page = client.get("/medicines").text
    home = page.split('id="home-h">At home</h2>')[1].split('id="clinic-h"')[0]
    clinic = page.split('id="clinic-h">At the clinic</h2>')[1].split("</section>")[0]
    # At home is what the family gives, grouped by type.
    assert home.index(">Nausea &amp; vomiting</h3>") < home.index(">White-blood-cell boosters</h3>")
    nausea = home.split(">Nausea &amp; vomiting</h3>")[1].split("</section>")[0]
    assert "Ondansetron" in nausea and "Dexamethasone" in nausea
    assert "Filgrastim" in home.split(">White-blood-cell boosters</h3>")[1]
    # The clinic's medicines are one plain list, not grouped by type.
    assert "Doxorubicin" in clinic and "<h3" not in clinic and "Doxorubicin" not in home


def test_log_a_dose_is_one_screen_grouped_like_the_medicines_page(client, cfg):
    import re
    setup_walkthrough(client, today(cfg))
    page = client.get("/dose/new").text  # "Took one earlier?" from Today
    form = page.split('action="/dose/new"')[1].split("</form>")[0]
    # Medicine, when, dose on one screen. No second page, no date-and-time picker to fight with.
    assert 'name="medication_id"' in form and 'name="ago"' in form and 'name="dose"' in form
    assert 'type="datetime-local"' not in form
    # Exact times only: Just now or Other time with a clock. No "30 min ago" guesses.
    assert re.findall(r'name="ago" value="([^"]+)"', form) == ["0", "pick"]
    assert ">Just now<" in form and ">Other time<" in form and " ago<" not in form
    groups = re.findall(r'<optgroup label="([^"]+)">', form)
    meds_page = client.get("/medicines").text
    home_heads = [h.replace("&amp;", "&") for h in re.findall(r'<h3 id="g-[^"]+">([^<]+)</h3>', meds_page)]
    assert [g.replace("&amp;", "&") for g in groups] == home_heads + ["At the clinic"]
    nausea = form.split('<optgroup label="Nausea &amp; vomiting">')[1].split("</optgroup>")[0]
    assert ">Ondansetron" in nausea and ">Dexamethasone" in nausea
    assert ">Doxorubicin" in form.split('<optgroup label="At the clinic">')[1]


def test_log_a_dose_with_a_medicine_already_picked(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT * FROM medications WHERE name = 'Prochlorperazine'").fetchone()
    page = client.get(f"/dose/new?med={med['id']}&back_to=/medicines/{med['id']}").text
    assert f'value="{med["id"]}" selected' in page and 'value="1 tablet"' in page  # dose from the label
    assert "Label: Example: 1 tablet every 6 hours as needed" in page and "OK to take now" in page
    assert f'href="/medicines/{med["id"]}"' in page
    r = client.post("/dose/new", data={"medication_id": med["id"], "ago": "60", "dose": "", "back": f"/medicines/{med['id']}"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/medicines/{med['id']}"
    with session(cfg.db_path) as conn:
        row = conn.execute("SELECT * FROM dose_logs WHERE medication_id = ?", (med["id"],)).fetchone()
    assert row["dose"] == "1 tablet"  # left blank, so the label's dose
    assert row["taken_at"][:13] == (local_now(cfg) - timedelta(minutes=60)).strftime("%Y-%m-%d %H")
    # Nothing picked: it says so instead of failing.
    r = client.post("/dose/new", data={"ago": "0", "back": "/"})
    assert r.status_code == 400 and "Pick the medicine first." in r.text


def test_medicine_page_shows_recent_doses_before_the_edit_forms(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT * FROM medications WHERE name = 'Ondansetron'").fetchone()
        log_id = store.log_dose(conn, med["id"], local_now(cfg) - timedelta(hours=1), "1 tablet", None)
    page = client.get(f"/medicines/{med['id']}").text
    assert page.index(">Recent doses</h2>") < page.index("Change details or schedule")
    # Its edit link comes back to this medicine, not Today.
    assert f'href="/dose/{log_id}?back_to=/medicines/{med["id"]}"' in page


def test_dose_page_puts_the_time_first_and_returns_where_it_came_from(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT * FROM medications WHERE name = 'Ondansetron'").fetchone()
        log_id = store.log_dose(conn, med["id"], local_now(cfg) - timedelta(hours=1), "1 tablet", None)
        seen = store.dose_log(conn, log_id)["updated_at"]
    page = client.get(f"/dose/{log_id}?back_to=/medicines/{med['id']}").text
    form = page.split('action="/dose/%d"' % log_id)[1].split("</form>")[0]
    assert 'name="taken_at"' in form and "<details" not in form.split('name="taken_at"')[0]
    assert "Change time or dose" not in page
    assert form.index('name="taken_at"') < form.index("Did it help?")
    assert f'href="/medicines/{med["id"]}"' in page and f'name="back" value="/medicines/{med["id"]}"' in page
    r = client.post(f"/dose/{log_id}", data={"seen": seen, "status": "taken", "taken_at": "2026-01-01T09:15",
                                             "back": f"/medicines/{med['id']}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/medicines/{med['id']}"
    with session(cfg.db_path) as conn:
        assert store.dose_log(conn, log_id)["taken_at"].startswith("2026-01-01 09:15")


def test_dose_page_asks_did_it_help_only_where_one_dose_can_be_judged(client, cfg):
    setup_walkthrough(client, today(cfg))
    when = local_now(cfg) - timedelta(hours=1)
    with session(cfg.db_path) as conn:
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM medications")}
        rescue = store.log_dose(conn, ids["Prochlorperazine"], when, "1 tablet", None)   # as-needed
        booster = store.log_dose(conn, ids["Filgrastim"], when, None, None)             # white-cell booster
        steroid = store.log_dose(conn, ids["Dexamethasone"], when, None, None)          # scheduled prevention
    assert "Did it help?" in client.get(f"/dose/{rescue}").text
    for log_id in (booster, steroid):
        page = client.get(f"/dose/{log_id}").text
        assert "Did it help?" not in page and 'name="taken_at"' in page and "Any side effects?" in page
    # An answer saved before this rule stays visible, so saving the page can't erase it.
    with session(cfg.db_path, write=True) as conn:
        conn.execute("UPDATE dose_logs SET helped = 'yes' WHERE id = ?", (booster,))
    assert "Did it help?" in client.get(f"/dose/{booster}").text


def test_clinic_medicine_asks_first_when_today_isnt_its_clinic_day(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))  # today is day 4; the clinic gives Doxorubicin on day 1
    with session(cfg.db_path) as conn:
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM medications")}
    page = client.get("/dose/new").text
    assert 'id="clinic-sheet"' in page and "Pick another medicine" in page
    dox = page.split(f'<option value="{ids["Doxorubicin"]}"')[1].split(">")[0]
    assert 'data-clinic-note="The infusion team gives Doxorubicin at the clinic on day 1. Today is day 4."' in dox
    assert "data-clinic-note" not in page.split(f'<option value="{ids["Ondansetron"]}"')[1].split(">")[0]
    # Without JavaScript the pop-up can't run, so saving asks on a check page first.
    r = client.post("/dose/new", data={"medication_id": ids["Doxorubicin"], "ago": "0", "back": "/"})
    assert "Given at the clinic" in r.text and "Today is day 4." in r.text and "Log it anyway" in r.text
    assert 'name="clinic_ok" value="1"' in r.text
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs WHERE medication_id = ?", (ids["Doxorubicin"],)).fetchone()[0] == 0
    # After "Log it anyway" (pop-up or check page) it logs.
    client.post("/dose/new", data={"medication_id": ids["Doxorubicin"], "ago": "0", "clinic_ok": "1", "back": "/"})
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs WHERE medication_id = ?", (ids["Doxorubicin"],)).fetchone()[0] == 1
    # A home medicine never asks.
    r = client.post("/dose/new", data={"medication_id": ids["Ondansetron"], "ago": "0", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_clinic_medicine_logs_without_asking_on_its_clinic_day(client, cfg):
    setup_walkthrough(client, today(cfg))  # today is day 1, chemo day
    with session(cfg.db_path) as conn:
        dox = conn.execute("SELECT id FROM medications WHERE name = 'Doxorubicin'").fetchone()["id"]
    page = client.get("/dose/new").text
    assert "data-clinic-note" not in page and 'id="clinic-sheet"' not in page
    r = client.post("/dose/new", data={"medication_id": dox, "ago": "0", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_how_was_the_dose_is_optional(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT id FROM medications WHERE name = 'Prochlorperazine'").fetchone()["id"]
        log_id = store.log_dose(conn, med, local_now(cfg) - timedelta(hours=1), "1 tablet", None)
        conn.execute("UPDATE dose_logs SET dose_feel = 'too_strong' WHERE id = ?", (log_id,))
        seen = store.dose_log(conn, log_id)["updated_at"]
    page = client.get(f"/dose/{log_id}").text
    assert "How was the dose? <span class=\"meta\">(optional)</span>" in page
    feel = page.split("How was the dose?")[1].split("</fieldset>")[0]
    assert "data-clearable" in feel and "required" not in feel
    # Saving with no answer picked clears it (what tapping the chosen answer again does).
    client.post(f"/dose/{log_id}", data={"seen": seen, "status": "taken", "taken_at": "2026-01-01T09:15", "back": "/"})
    with session(cfg.db_path) as conn:
        assert store.dose_log(conn, log_id)["dose_feel"] is None


def test_taken_asks_just_now_or_other_time(client, cfg):
    import re
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # day 3: dexamethasone 8:00, olanzapine 21:00
    page = client.get("/").text
    row = page.split('id="slot-')[1:]
    dex = next(r for r in row if ">Dexamethasone</a>" in r.split("</div>")[0] or ">Dexamethasone</a>" in r[:400])
    panel = dex.split('<details class="take"')[1].split("</details>")[0]
    # Exact times only: the planned time records the schedule, not when it was taken.
    assert "When was it taken?" in panel and 'name="at_time"' in panel
    assert re.findall(r'name="when" value="([^"]+)"', panel) == ["now", "at"]
    assert panel.index("Just now") < panel.index("Other time") and 'value="planned"' not in panel
    # A past day has no "Just now". Only the clock, set to the planned time to check.
    yesterday = (today(cfg) - timedelta(days=1)).isoformat()
    past = client.get(f"/?d={yesterday}").text.split('<details class="take"')[1].split("</details>")[0]
    assert re.findall(r'name="when" value="([^"]+)"', past) == ["at"] and "Just now" not in past


def test_taken_at_the_planned_or_another_time(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    with session(cfg.db_path) as conn:
        cycle = schedule.cycle_for_date(conn, today(cfg))
        dex = next(s for s in schedule.slots(conn, cycle, 3) if s.plan["med_name"] == "Dexamethasone")
        olz = next(s for s in schedule.slots(conn, cycle, 2) if s.plan["med_name"] == "Olanzapine")  # yesterday 21:00
    tap = {"plan_id": dex.plan["id"], "slot": dex.key, "status": "taken", "back": "/"}
    if local_now(cfg) >= dex.dt:
        client.post("/slot", data={**tap, "when": "planned"})
        with session(cfg.db_path) as conn:
            assert store.slot_log(conn, dex.plan["id"], dex.key)["taken_at"].startswith(dex.dt.strftime("%Y-%m-%d %H:%M"))
            store.delete_dose_log(conn, store.slot_log(conn, dex.plan["id"], dex.key)["id"])
    # A time that hasn't happened yet is refused, not logged.
    later = (local_now(cfg) + timedelta(hours=2))
    if later.date() == local_now(cfg).date():
        r = client.post("/slot", data={**tap, "when": "at", "at_time": later.strftime("%H:%M")}, follow_redirects=False)
        assert r.status_code == 303
        with session(cfg.db_path) as conn:
            assert store.slot_log(conn, dex.plan["id"], dex.key) is None
    # Yesterday's bedtime dose logged at 12:30 means after midnight, not the morning before.
    client.post("/slot", data={"plan_id": olz.plan["id"], "slot": olz.key, "status": "taken", "back": "/",
                               "when": "at", "at_time": "21:40"})
    with session(cfg.db_path) as conn:
        assert store.slot_log(conn, olz.plan["id"], olz.key)["taken_at"].startswith(f"{olz.date.isoformat()} 21:40")


def test_chosen_time_after_midnight_for_evening_doses():
    from datetime import datetime as DT
    from chemotracker.web import _chosen_time

    class FakeSlot:
        date = DT(2026, 1, 2).date()
        time = "21:00"
        dt = DT(2026, 1, 2, 21, 0)
    now = DT(2026, 1, 3, 1, 0)
    assert _chosen_time("at", FakeSlot, "00:30", now) == DT(2026, 1, 3, 0, 30)
    assert _chosen_time("at", FakeSlot, "22:15", now) == DT(2026, 1, 2, 22, 15)
    assert _chosen_time("planned", FakeSlot, None, now) == DT(2026, 1, 2, 21, 0)
    assert _chosen_time("now", FakeSlot, None, now) == now


def test_clinic_doses_ask_when_and_show_the_time(client, cfg):
    # Chemo's time starts the 24 hours before a white-cell booster may be given, so clinic doses get an
    # exact time like home doses, and every place that lists them shows it.
    setup_walkthrough(client, today(cfg) - timedelta(days=1))  # day 2: yesterday was the chemo day
    yesterday = (today(cfg) - timedelta(days=1)).isoformat()
    page = client.get(f"/?d={yesterday}").text
    clinic = page.split('id="c-clinic"')[1].split("</details>\n")[0]
    assert "When was it given?" in clinic and "Given</span>" in clinic
    # A past day: no "Just now", only the clock.
    first = clinic.split('<details class="take"')[1].split("</details>")[0]
    assert re.findall(r'name="when" value="([^"]+)"', first) == ["at"] and "Just now" not in first
    with session(cfg.db_path) as conn:
        cycle = schedule.cycle_for_date(conn, today(cfg))
        dox = next(s for s in schedule.slots(conn, cycle, 1) if s.plan["med_name"] == "Doxorubicin")
    r = client.post("/slot", data={"plan_id": dox.plan["id"], "slot": dox.key, "status": "taken", "when": "at",
                                   "at_time": "13:45", "back": f"/?d={yesterday}"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        assert store.slot_log(conn, dox.plan["id"], dox.key)["taken_at"] == f"{yesterday} 13:45"
    shown = client.get(f"/?d={yesterday}").text
    assert "Doxorubicin marked given at 1:45 PM." in shown and "Given 1:45 PM" in shown
    assert "Given 1:45 PM" in client.get("/cycles/1?day=1").text  # the day list
    assert "✓ 1:45p" in client.get("/cycles/1").text  # the sheet
    csv = client.get("/report-doses.csv").content.decode("utf-8-sig")
    dox_rows = [line for line in csv.splitlines() if ",Doxorubicin (" in line]
    assert dox_rows and dox_rows[0].startswith(f"{yesterday},13:45,")


def test_todays_clinic_doses_offer_just_now_or_other_time(client, cfg):
    setup_walkthrough(client, today(cfg))  # day 1: chemo day
    clinic = client.get("/").text.split('id="c-clinic"')[1].split("</details>\n")[0]
    panel = clinic.split('<details class="take"')[1].split("</details>")[0]
    assert "When was it given?" in panel and re.findall(r'name="when" value="([^"]+)"', panel) == ["now", "at"]


def test_took_all_logs_every_open_dose_once(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))  # day 4: yesterday's doses are all open
    with session(cfg.db_path) as conn:
        cycle = schedule.cycle_for_date(conn, today(cfg))
        past = [s for s in schedule.slots(conn, cycle, 3) if s.kind != "clinic"]
        first = past[0]
        store.log_slot(conn, first.plan, first.key, "taken", first.dt, None)  # someone already logged one
    items = [f"{s.plan['id']}|{s.key}" for s in past]
    r = client.post("/slots/taken", data={"slots[]": items, "when": "planned", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        logs = [store.slot_log(conn, s.plan["id"], s.key) for s in past]
        assert all(l is not None for l in logs)
        assert all(l["taken_at"].startswith(s.dt.strftime("%Y-%m-%d %H:%M")) for l, s in zip(logs, past))
        new_ids = [l["id"] for l in logs[1:]]
    toast = client.get("/").text
    assert "action=\"/doses/undo\"" in toast and "already logged by someone else" in toast
    client.post("/doses/undo", data={"ids[]": [str(i) for i in new_ids], "back": "/"})
    with session(cfg.db_path) as conn:
        assert store.slot_log(conn, first.plan["id"], first.key) is not None  # theirs stays
        assert all(store.slot_log(conn, s.plan["id"], s.key) is None for s in past[1:])


def test_took_both_shows_only_where_two_open_doses_share_a_compartment(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))  # day 4
    yesterday = (today(cfg) - timedelta(days=1)).isoformat()
    page = client.get(f"/?d={yesterday}").text
    assert "When were they taken?" not in page  # one dose per compartment: each row's own Taken is enough
    with session(cfg.db_path, write=True) as conn:
        conn.execute("""INSERT INTO plans (medication_id, kind, start_day, end_day, times, from_cycle, created_at)
                        SELECT id, 'scheduled', 1, NULL, '08:00', 1, datetime('now') FROM medications
                        WHERE name = 'Olanzapine'""")
    morning = client.get(f"/?d={yesterday}").text.split('id="c-morning"')[1].split("</details>\n        ")[0]
    assert "Took both" in morning and "When were they taken?" in morning
    assert morning.count('name="slots[]"') == 2


def test_took_one_asks_when_and_checks_spacing_at_that_time(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # day 3: prochlorperazine every 6 hours
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT id FROM medications WHERE name = 'Prochlorperazine'").fetchone()["id"]
    page = client.get("/").text
    row = page.split(">Prochlorperazine</a>")[1].split("</form>")[0]
    # Just now or Other time, equally quiet. The logged time sets when the next one is OK, so no guesses.
    assert "When did you take it?" in row and "btn-primary" not in row and " ago<" not in row
    assert 'value="0">Just now' in row and ">Other time<" in row and 'name="pick_time"' in row
    hour_ago = local_now(cfg) - timedelta(minutes=60)
    client.post("/dose/quick", data={"medication_id": med, "ago": "pick", "pick_time": hour_ago.strftime("%H:%M"), "back": "/"})
    with session(cfg.db_path) as conn:
        first = conn.execute("SELECT taken_at FROM dose_logs WHERE medication_id = ?", (med,)).fetchone()["taken_at"]
    assert first.startswith(hour_ago.strftime("%Y-%m-%d %H:%M"))
    # A second one "just now" is 1 hour after the first: the label says every 6 hours, so it asks first.
    r = client.post("/dose/quick", data={"medication_id": med, "ago": "0", "back": "/"})
    assert "Check before you log this" in r.text or "sooner" in r.text.lower()
    assert 'name="taken_at"' in r.text and 'name="ago"' not in r.text


def test_rows_just_logged_get_marked_once(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=3))  # day 4
    yesterday = (today(cfg) - timedelta(days=1)).isoformat()
    with session(cfg.db_path) as conn:
        cycle = schedule.cycle_for_date(conn, today(cfg))
        dex = next(s for s in schedule.slots(conn, cycle, 3) if s.plan["med_name"] == "Dexamethasone")
    client.post("/slot", data={"plan_id": dex.plan["id"], "slot": dex.key, "status": "taken", "when": "planned",
                               "back": f"/?d={yesterday}"}, follow_redirects=False)
    first = client.get(f"/?d={yesterday}").text
    row = first.split(f'id="slot-{dex.plan["id"]}-')[0].rsplit('<div class="dose ', 1)[1]
    assert "just-logged" in row
    again = client.get(f"/?d={yesterday}").text  # the moment plays once, not on every visit
    assert "just-logged" not in again


def test_medicines_show_when_each_is_used_as_time_of_day_chips(client, cfg):
    setup_walkthrough(client, today(cfg))
    page = client.get("/medicines").text
    dex = page.split(">Dexamethasone</span>")[1].split("</li>")[0]
    assert 'data-period="morning"' in dex and "8:00 AM" in dex and "Days 2–4" in dex
    olz = page.split(">Olanzapine</span>")[1].split("</li>")[0]
    assert 'data-period="bedtime"' in olz
    ond = page.split(">Ondansetron</span>")[1].split("</li>")[0]
    assert 'data-period="prn"' in ond and "As needed" in ond
    assert "Scheduled, days" not in page  # the chips replace that text


def test_reminder_problems_are_explained_in_plain_words():
    from chemotracker import reminders
    assert "didn't answer" in reminders.problem_text("couldn't reach https://ntfy.sh (timed out)")
    assert "access token" in reminders.problem_text("ntfy answered 403: forbidden")
    assert "start again on their own" in reminders.problem_text("ntfy answered 429")
    assert "try again" in reminders.problem_text("something odd")


def test_side_effect_chips_hide_the_native_checkbox(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT id FROM medications WHERE name = 'Ondansetron'").fetchone()["id"]
        log_id = store.log_dose(conn, med, local_now(cfg) - timedelta(hours=1), "1 tablet", None)
    page = client.get(f"/dose/{log_id}").text
    chips = page.split("Any side effects?")[1].split("</fieldset>")[0]
    assert 'class="chip-toggle"' in chips and 'class="check"' not in chips


def test_add_custom_medicine(client, cfg):
    setup_walkthrough(client, today(cfg))
    r = client.post("/medicines/add", data={"custom_name": "Magic mouthwash"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/medicines/")
    assert "Magic mouthwash" in client.get("/medicines").text


def test_moving_day1_moves_the_schedule(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    with session(cfg.db_path) as conn:
        c = schedule.all_cycles(conn)[0]
    client.post(f"/cycles/{c['id']}", data={"number": "1", "day1": (today(cfg) - timedelta(days=10)).isoformat(),
                                            "length_days": "14"})
    html = client.get("/").text
    assert "Day 11" in html and "Dexamethasone" not in html.split("today-aside")[0].split("feel")[-1]


def test_excel_report_and_csv(client, cfg):
    day1 = today(cfg) - timedelta(days=4)
    setup_walkthrough(client, day1)
    with session(cfg.db_path) as conn:
        cycle = schedule.cycle_for_date(conn, today(cfg))
        dex = next(s for s in schedule.slots(conn, cycle, 2) if s.plan["med_name"] == "Dexamethasone")
        store.log_slot(conn, dex.plan, dex.key, "taken", dex.dt + timedelta(minutes=12), dex.dose)
        store.add_symptom(conn, {"symptom": "nausea", "severity": "3"}, local_now(cfg))
    r = client.get(f"/report.xlsx?cycle={cycle['id']}")
    assert r.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[:4] == ["Summary", "Daily overview", "Dose log", "Symptoms"]
    assert "Cycle 1 grid" in wb.sheetnames and "Medicines" in wb.sheetnames
    log = wb["Dose log"]
    statuses = [log.cell(row=i, column=10).value for i in range(5, log.max_row + 1)]
    assert "Taken" in statuses and "Not logged" in statuses  # gaps are visible
    meds = wb["Medicines"]
    labels = [meds.cell(row=i, column=4).value for i in range(5, meds.max_row + 1)]
    assert "Example: 1 tablet every 8 hours as needed" in labels
    sym = wb["Symptoms"]
    assert sym.cell(row=5, column=5).value == "Nausea" and sym.cell(row=5, column=6).value == "Severe"

    csv_text = client.get(f"/report-doses.csv?cycle={cycle['id']}").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("Date,Time,Planned time,Cycle,Day,Medicine")
    assert re.search(r"Dexamethasone.*Taken", csv_text)
    sym_csv = client.get(f"/report-symptoms.csv?cycle={cycle['id']}").content.decode("utf-8-sig")
    assert "Nausea,Severe" in sym_csv


def test_backup_writes_snapshot_and_prunes(cfg, client):
    setup_walkthrough(client, today(cfg))
    for _ in range(3):
        result = backup.run_backup(cfg, "test")
        assert result.ok and result.path and result.path.exists()
        assert result.path.with_suffix(".xlsx").exists()
    files = backup.list_backups(cfg.backup_dir)
    assert 1 <= len(files) <= cfg.backup_keep
    from dataclasses import replace

    small = replace(cfg, backup_keep=1)
    backup.prune(cfg.backup_dir, small.backup_keep)
    assert len(backup.list_backups(cfg.backup_dir)) == 1
    import sqlite3

    snap = sqlite3.connect(backup.list_backups(cfg.backup_dir)[0])
    assert snap.execute("SELECT COUNT(*) FROM medications").fetchone()[0] == 8
    snap.close()
    page = client.get("/settings").text
    assert "Last good backup" in page and "None yet" not in page


def test_backup_schedule_math(cfg):
    from dataclasses import replace
    from datetime import datetime, time

    daily = replace(cfg, backup_interval_hours=24, backup_time=time(3, 0))
    assert backup.next_run(daily, datetime(2026, 10, 9, 20, 15)) == datetime(2026, 10, 10, 3, 0)
    assert backup.next_run(daily, datetime(2026, 10, 10, 2, 59)) == datetime(2026, 10, 10, 3, 0)
    six = replace(cfg, backup_interval_hours=6, backup_time=time(3, 0))
    assert backup.next_run(six, datetime(2026, 10, 9, 10, 0)) == datetime(2026, 10, 9, 15, 0)
    assert backup.is_overdue(daily, datetime(2026, 10, 9, 20, 0))  # never backed up


def test_password_gate(cfg, monkeypatch):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from chemotracker.web import create_app

    locked = replace(cfg, password="family")
    with TestClient(create_app(locked, start_scheduler=False)) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert c.get("/healthz").status_code == 200
        assert c.post("/login", data={"password": "nope", "next": "/"}).status_code == 401
        r = c.post("/login", data={"password": "family", "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/", follow_redirects=False).headers["location"] == "/setup"


def test_password_is_optional(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from chemotracker.config import load_settings
    from chemotracker.web import create_app

    # Unset, empty, or blank all mean no sign-in screen.
    for value in (None, "", "   "):
        if value is None:
            monkeypatch.delenv("CHEMO_PASSWORD", raising=False)
        else:
            monkeypatch.setenv("CHEMO_PASSWORD", value)
        cfg = load_settings(tmp_path / f"d{len(value or 'x')}", tmp_path / "b")
        assert cfg.password is None
        with TestClient(create_app(cfg, start_scheduler=False)) as c:
            assert c.get("/", follow_redirects=False).headers["location"] == "/setup"
            assert c.get("/login", follow_redirects=False).status_code == 303


def test_every_page_renders(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))
    with session(cfg.db_path) as conn:
        med_id = conn.execute("SELECT id FROM medications LIMIT 1").fetchone()[0]
        cycle_id = conn.execute("SELECT id FROM cycles LIMIT 1").fetchone()[0]
        entry = store.add_symptom(conn, {"symptom": "pain", "value": "4", "notes": "back"}, local_now(cfg))
        log = store.log_dose(conn, med_id, local_now(cfg), "1", None)
    for url in ["/", f"/?d={(today(cfg) + timedelta(days=3)).isoformat()}", "/symptoms", "/symptoms/new?s=nausea",
                "/symptoms/new?s=temperature", "/symptoms/new?s=pain", "/symptoms/new?s=vomiting",
                "/symptoms/new?s=other", f"/symptoms/{entry}", f"/symptoms/{entry}/saved", "/medicines",
                "/medicines/add", f"/medicines/{med_id}", "/cycles", f"/cycles/{cycle_id}", "/report",
                "/report?cycle=&start=2026-01-01&end=2026-01-31", "/settings", "/setup", "/setup/medicines",
                "/setup/label/0", "/setup/cycle", "/setup/team", f"/dose/{log}", f"/dose/new?med={med_id}",
                "/manifest.webmanifest", "/who", "/people", "/setup/people"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:500])
    assert client.get("/medicines/9999").status_code == 404


def test_entries_record_who_logged_them(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))
    with session(cfg.db_path) as conn:
        people = {p["name"]: p["id"] for p in conn.execute("SELECT * FROM profiles")}
        cycle = schedule.cycle_for_date(conn, today(cfg))
        dex = next(s for s in schedule.slots(conn, cycle, 3) if s.plan["med_name"] == "Dexamethasone")
    assert set(people) == {"John", "Jane"}
    # John (this device) logs a dose.
    client.post("/slot", data={"plan_id": dex.plan["id"], "slot": dex.key, "status": "taken", "back": "/"})
    # Jane switches in on the same device, logs a symptom, and answers the dose follow-up.
    r = client.post("/who", data={"profile_id": people["Jane"], "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/symptoms/new", data={"symptom": "fatigue", "severity": "1"})
    with session(cfg.db_path) as conn:
        log = conn.execute("SELECT * FROM dose_logs WHERE plan_id = ?", (dex.plan["id"],)).fetchone()
        sym = conn.execute("SELECT * FROM symptom_logs").fetchone()
    assert log["logged_by"] == people["John"] and log["updated_by"] is None
    assert sym["logged_by"] == people["Jane"]
    client.post(f"/dose/{log['id']}/follow", data={"helped": "yes", "dose_feel": "right", "back": "/"})
    with session(cfg.db_path) as conn:
        log = store.dose_log(conn, log["id"])
    assert log["logged_by"] == people["John"] and log["updated_by"] == people["Jane"]

    page = client.get("/").text
    assert 'class="by"' in page and "John" in page and "edited by Jane" in page
    assert "Tired · Mild" in page and "Jane" in client.get("/symptoms").text

    wb = openpyxl.load_workbook(io.BytesIO(client.get(f"/report.xlsx?cycle={cycle['id']}").content))
    dl = wb["Dose log"]
    assert dl.cell(row=4, column=16).value == "Logged by"
    by_values = [dl.cell(row=i, column=16).value for i in range(5, dl.max_row + 1)]
    assert "John (edited by Jane)" in by_values
    sy = wb["Symptoms"]
    assert sy.cell(row=4, column=9).value == "Logged by" and sy.cell(row=5, column=9).value == "Jane"
    summary = [c.value for row in wb["Summary"].iter_rows() for c in row if c.value]
    assert "Who logged entries" in summary
    csv_text = client.get(f"/report-doses.csv?cycle={cycle['id']}").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].endswith(",Logged by") and "John (edited by Jane)" in csv_text


def test_new_device_is_asked_who_it_is(cfg):
    from fastapi.testclient import TestClient

    from chemotracker.web import create_app

    app = create_app(cfg, start_scheduler=False)
    with TestClient(app) as first:
        setup_walkthrough(first, today(cfg))
    with TestClient(app) as second:  # a different phone: no cookie yet
        r = second.get("/symptoms", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/who?next=%2Fsymptoms")
        page = second.get("/who").text
        assert "John" in page and "Jane" in page
        with session(cfg.db_path) as conn:
            jane = conn.execute("SELECT id FROM profiles WHERE name = 'Jane'").fetchone()["id"]
        r = second.post("/who", data={"profile_id": jane, "next": "/symptoms"}, follow_redirects=False)
        assert r.headers["location"] == "/symptoms"
        assert second.get("/symptoms").status_code == 200
        assert "Using as" in second.get("/").text


def test_people_can_be_added_renamed_and_removed(client, cfg):
    setup_walkthrough(client, today(cfg))
    client.post("/people", data={"name": "Sam", "role": "caregiver"})
    r = client.post("/people", data={"name": "sam", "role": "family"}, follow_redirects=True)
    assert "already someone called" in r.text
    with session(cfg.db_path) as conn:
        sam = conn.execute("SELECT * FROM profiles WHERE name = 'Sam'").fetchone()
        med_id = conn.execute("SELECT id FROM medications LIMIT 1").fetchone()[0]
    assert sam["role"] == "caregiver"
    client.post("/who", data={"profile_id": sam["id"], "next": "/"})
    client.post("/dose/quick", data={"medication_id": med_id, "back": "/"})
    client.post(f"/people/{sam['id']}", data={"name": "Sam R", "role": "caregiver"})
    client.post(f"/people/{sam['id']}/active", data={"active": "0"})
    # Removed people keep their name on past entries, and this device must pick again.
    with session(cfg.db_path) as conn:
        assert profiles.name_of(profiles.by_id(conn), conn.execute(
            "SELECT logged_by FROM dose_logs").fetchone()[0]) == "Sam R"
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"].startswith("/who")
    page = client.get("/people").text
    assert "Removed" in page and "Sam R" in page


def test_single_person_is_selected_automatically(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        jane = conn.execute("SELECT id FROM profiles WHERE name = 'Jane'").fetchone()["id"]
        profiles.set_active(conn, jane, False)
    client.cookies.clear()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200 and "John" in r.text


def test_v1_database_upgrades_in_place(tmp_path):
    import sqlite3

    from chemotracker.db import MIGRATIONS, migrate

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
    conn.execute("INSERT INTO medications (name, created_at, updated_at) VALUES ('X', 'now', 'now')")
    conn.execute("INSERT INTO dose_logs (medication_id, status, taken_at, created_at, updated_at) "
                 "VALUES (1, 'taken', '2026-01-01 08:00', 'now', 'now')")
    conn.commit()
    conn.close()
    assert migrate(path) == len(MIGRATIONS)
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dose_logs)")}
    assert {"logged_by", "updated_by"} <= cols
    assert conn.execute("SELECT logged_by FROM dose_logs").fetchone()[0] is None
    conn.close()


def test_install_without_people_prompts_to_add_them(client, cfg):
    setup_walkthrough(client, today(cfg))
    with session(cfg.db_path) as conn:
        conn.execute("UPDATE dose_logs SET logged_by = NULL")
        conn.execute("DELETE FROM profiles")
    client.cookies.clear()
    page = client.get("/")
    assert page.status_code == 200 and "Add who's using ChemoTracker" in page.text


def _setup_until_medicines(client, day1, **cycle):
    client.post("/setup/people", data={"names[]": ["John"], "roles[0]": "patient", "me": "new:0"})
    data = {"number": "1", "day1": day1.isoformat(), "length_days": "14", "count": "4", "regimen": "AC", **cycle}
    r = client.post("/setup/cycle", data=data, follow_redirects=False)
    assert r.headers["location"] == "/setup/medicines", r.text


def test_setup_reads_pasted_prescriptions(client, cfg):
    from chemotracker.web import RX_EXAMPLE

    day1 = today(cfg)
    _setup_until_medicines(client, day1)
    page = client.get("/setup/medicines").text
    assert "Write in your prescriptions" in page and "See an example" in page and "OLANZapine 5MG TAB NORT" in page

    preview = client.post("/rx/preview", data={"rx_text": RX_EXAMPLE})
    assert preview.status_code == 200
    html = preview.text
    assert 'data-keys="ondansetron,prochlorperazine,dexamethasone,olanzapine"' in html
    assert "Days 2–4" in html and schedule.fmt_day(day1 + timedelta(days=1)) in html  # real dates from chemo day
    assert "at least 8 hours apart" in html and "guess" in html

    # No-JS path: "Read it" re-renders the page with the preview and ticks the matches.
    r = client.post("/setup/medicines", data={"rx_text": RX_EXAMPLE, "action": "preview"})
    assert r.status_code == 200 and "Found 4 medicines" in r.text
    assert 'value="olanzapine" checked' in r.text

    # Save with olanzapine unticked in the preview, plus one ticked from the list.
    r = client.post("/setup/medicines", data={"rx_text": RX_EXAMPLE, "rx_seen": "1", "rx_include[]": ["0", "1", "2"],
                                              "keys[]": ["doxorubicin"]}, follow_redirects=False)
    assert r.headers["location"] == "/setup/label/0"
    with session(cfg.db_path) as conn:
        meds = {m["info_key"]: m for m in store.medications(conn)}
        assert set(meds) == {"ondansetron", "prochlorperazine", "dexamethasone", "doxorubicin"}
        dex = meds["dexamethasone"]
        assert (dex["strength"], dex["dose"]) == ("4 mg tablet", "2 tablets (8 mg)")
        assert [(p["kind"], p["start_day"], p["end_day"], p["times"]) for p in store.plans(conn, dex["id"])] == \
            [("scheduled", 2, 4, "08:00")]
        assert meds["ondansetron"]["min_hours_between"] == 8
        # The pasted text itself is never stored.
        dump = " ".join(str(v) for row in conn.execute("SELECT * FROM medications") for v in tuple(row))
        assert "NORT" not in dump


def test_two_part_regimen_and_cycle_scoped_prescriptions(client, cfg):
    day1 = today(cfg)
    _setup_until_medicines(client, day1, then_regimen="Paclitaxel", then_count="12", then_length_days="7")
    with session(cfg.db_path) as conn:
        cycles = schedule.all_cycles(conn)
    assert [c["number"] for c in cycles] == list(range(1, 17))
    assert cycles[3]["day1"] == (day1 + timedelta(days=42)).isoformat()
    assert cycles[4]["day1"] == (day1 + timedelta(days=56)).isoformat() and cycles[4]["length_days"] == 7
    assert cycles[15]["regimen"] == "Paclitaxel"
    text = "Olanzapine 5 mg tab - Take 1 tablet at bedtime on days 1 to 4"
    html = client.post("/rx/preview", data={"rx_text": text}).text
    assert "AC · cycles 1–4" in html and "Paclitaxel · cycles 5–16" in html
    client.post("/setup/medicines", data={"rx_text": text, "rx_seen": "1", "rx_include[]": ["0"], "rx_cycles_0": "1-4"})
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT id FROM medications WHERE info_key = 'olanzapine'").fetchone()
        plan = store.plans(conn, med["id"])[0]
    assert (plan["from_cycle"], plan["to_cycle"]) == (1, 4)


def test_add_medicine_and_fill_from_label(client, cfg):
    setup_walkthrough(client, today(cfg))
    r = client.post("/medicines/add", data={"rx_text": "Lorazepam 0.5 mg tab - Take 1 tablet by mouth every 6 hours as needed for anxiety"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/medicines/")
    with session(cfg.db_path) as conn:
        lor = conn.execute("SELECT * FROM medications WHERE info_key = 'lorazepam'").fetchone()
        olz = conn.execute("SELECT * FROM medications WHERE info_key = 'olanzapine'").fetchone()
    assert lor["min_hours_between"] == 6 and lor["strength"] == "0.5 mg tablet"
    r = client.post(f"/medicines/{olz['id']}/import",
                    data={"rx_text": "Olanzapine 10 mg tab - Take 1 tablet by mouth at 8 PM on days 1 to 3"}, follow_redirects=True)
    assert "Filled in from the label" in r.text
    with session(cfg.db_path) as conn:
        olz = store.medication(conn, olz["id"])
        plans = store.plans(conn, olz["id"])
    assert olz["strength"] == "10 mg tablet" and [(p["start_day"], p["end_day"], p["times"]) for p in plans] == [(1, 3, "20:00")]


def test_taper_doses_show_on_today(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=1))  # today is Day 2
    text = "Dexamethasone 4 mg tab - Take 1 tablet the evening of chemo, then 2 tablets twice daily for 3 days"
    with session(cfg.db_path) as conn:
        dex = conn.execute("SELECT id FROM medications WHERE info_key = 'dexamethasone'").fetchone()["id"]
    client.post(f"/medicines/{dex}/import", data={"rx_text": text})
    html = client.get("/").text
    assert "2 tablets (8 mg)" in html
    with session(cfg.db_path) as conn:
        day1 = [s for s in schedule.slots(conn, schedule.all_cycles(conn)[0], 1) if s.plan["medication_id"] == dex]
    assert [(s.time, s.dose) for s in day1] == [("18:00", "1 tablet (4 mg)")]


def test_as_needed_too_soon_asks_first(client, cfg):
    setup_walkthrough(client, today(cfg) - timedelta(days=2))  # Day 3, prochlorperazine every 6 h
    with session(cfg.db_path) as conn:
        med = conn.execute("SELECT id FROM medications WHERE info_key = 'prochlorperazine'").fetchone()["id"]
    client.post("/dose/quick", data={"medication_id": med, "back": "/"})
    r = client.post("/dose/quick", data={"medication_id": med, "back": "/"})
    assert r.status_code == 200 and "Check before you log this" in r.text and "6 hours" in r.text
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs WHERE medication_id = ?", (med,)).fetchone()[0] == 1
    r = client.post("/dose/quick", data={"medication_id": med, "back": "/", "confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dose_logs WHERE medication_id = ?", (med,)).fetchone()[0] == 2
    future = (local_now(cfg) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    r = client.post("/dose/new", data={"medication_id": med, "taken_at": future})
    assert r.status_code == 400 and "hasn&#39;t happened yet" in r.text


def test_temperature_units(client, cfg):
    setup_walkthrough(client, today(cfg))
    # Default °F; a reading in °C is recognised and converted, with a note.
    r = client.post("/symptoms/new", data={"symptom": "temperature", "value": "38.2"}, follow_redirects=False)
    saved = client.get(r.headers["location"]).text
    assert "100.8\u00a0°F" in saved and "callout-urgent" in saved
    with session(cfg.db_path) as conn:
        row = conn.execute("SELECT * FROM symptom_logs").fetchone()
    assert row["value"] == 100.8 and row["notes"] == "Entered as 38.2 °C."
    # Switch to °C: the fever line is typed in °C and stored in °F, readings show in °C.
    r = client.post("/settings", data={"temp_unit": "C", "fever_line": "38.5", "call_if": "Fever"}, follow_redirects=False)
    assert r.status_code == 303
    with session(cfg.db_path) as conn:
        prefs = get_settings(conn)
    assert prefs["temp_unit"] == "C" and float(prefs["fever_f"]) == 101.3
    page = client.get("/symptoms").text
    assert "38.2\u00a0°C" in page and "°F" not in page.split("What are you noticing")[1]
    form = client.get("/symptoms/new?s=temperature").text
    assert "Temperature (°C)" in form and "38.5\u00a0°C" in form
    client.post("/symptoms/new", data={"symptom": "temperature", "value": "37.1"})
    with session(cfg.db_path) as conn:
        latest = conn.execute("SELECT * FROM symptom_logs ORDER BY id DESC").fetchone()
    assert latest["value"] == 98.8 and latest["notes"] is None  # typed in the chosen unit, no note
    r = client.post("/symptoms/new", data={"symptom": "temperature", "value": "12"})
    assert r.status_code == 400 and "body temperature" in r.text
    bad = client.post("/settings", data={"temp_unit": "C", "fever_line": "45"}, follow_redirects=True)
    assert "fever line should be between" in bad.text
    wb = openpyxl.load_workbook(io.BytesIO(client.get("/report.xlsx").content))
    daily = wb["Daily overview"]
    assert daily.cell(row=4, column=8).value == "Highest temp °C"
    assert "38.2\u00a0°C" in [wb["Symptoms"].cell(row=i, column=7).value for i in range(5, wb["Symptoms"].max_row + 1)]


def test_moving_chemo_moves_later_cycles(client, cfg):
    day1 = today(cfg)
    client.get("/")
    client.post("/setup/people", data={"names[]": ["John"], "roles[0]": "patient", "me": "new:0"})
    client.post("/setup/cycle", data={"number": "1", "day1": day1.isoformat(), "length_days": "14", "count": "4"})
    with session(cfg.db_path) as conn:
        c2 = [c for c in schedule.all_cycles(conn) if c["number"] == 2][0]
    page = client.get(f"/cycles/{c2['id']}?change=1").text
    assert "Chemo moved? Change the date" in page and "Move the 2 cycles after this one too" in page
    new = (date.fromisoformat(c2["day1"]) + timedelta(days=7)).isoformat()
    r = client.post(f"/cycles/{c2['id']}", data={"day1": new, "number": "2", "length_days": "14", "shift_later": "1"},
                    follow_redirects=True)
    assert "moved 7 days later too" in r.text
    with session(cfg.db_path) as conn:
        days = {c["number"]: c["day1"] for c in schedule.all_cycles(conn)}
    assert days == {1: day1.isoformat(), 2: new, 3: (day1 + timedelta(days=35)).isoformat(),
                    4: (day1 + timedelta(days=49)).isoformat()}
    # Only this one: later cycles stay put.
    client.post(f"/cycles/{c2['id']}", data={"day1": c2["day1"], "number": "2", "length_days": "14"})
    with session(cfg.db_path) as conn:
        assert {c["number"]: c["day1"] for c in schedule.all_cycles(conn)}[3] == (day1 + timedelta(days=35)).isoformat()


def test_medicines_list_and_page_for_non_technical_users(client, cfg):
    setup_walkthrough(client, today(cfg))
    page = client.get("/medicines").text
    # Clinic infusions come with no bottle, so no "copy the label" nudge.
    clinic_part = page.split('id="clinic-h">At the clinic</h2>')[1].split("</section>")[0]
    assert "Doxorubicin" in clinic_part and 'class="tchip" data-period="clinic"' in clinic_part
    assert "Add the label details" not in clinic_part
    with session(cfg.db_path) as conn:
        olz = conn.execute("SELECT id FROM medications WHERE info_key = 'olanzapine'").fetchone()["id"]
        dox = conn.execute("SELECT id FROM medications WHERE info_key = 'doxorubicin'").fetchone()["id"]
    detail = client.get(f"/medicines/{olz}").text
    assert "What you take" in detail and "Change details or schedule" in detail
    assert '<details class="edit-zone" id="edit" >' in detail or '<details class="edit-zone" id="edit">' in detail  # folded
    assert "What the clinic gives" in client.get(f"/medicines/{dox}").text
    assert 'id="edit" open' in client.get(f"/medicines/{olz}?edit=1").text


def test_prescription_preview_says_when_it_updates(client, cfg):
    setup_walkthrough(client, today(cfg))
    html = client.post("/rx/preview", data={"rx_text": "Olanzapine 5 mg tab - Take 1 tablet at bedtime"}).text
    assert "Already on your list · saving updates it" in html
    html = client.post("/rx/preview", data={"rx_text": "Lorazepam 0.5 mg tab - Take 1 tablet every 6 hours as needed"}).text
    assert "In the medicine list" in html


def test_rerunning_setup_names_what_it_stops(client, cfg):
    setup_walkthrough(client, today(cfg))
    keys = ["doxorubicin", "cyclophosphamide", "paclitaxel", "ondansetron", "prochlorperazine", "dexamethasone", "olanzapine"]
    r = client.post("/setup/medicines", data={"keys[]": keys}, follow_redirects=True)
    assert "Stopped Filgrastim because it was unticked" in r.text


def test_header_has_a_call_button(client, cfg):
    setup_walkthrough(client, today(cfg))
    # From 9 PM to 7 AM the header calls the after-hours line (base.html night_call).
    hour = local_now(cfg).hour
    number = "555-0199" if hour >= 21 or hour < 7 else "555-0100"
    assert f'class="icon-link call-link" href="tel:{number}"' in client.get("/").text


def test_theme_controls(client, cfg):
    setup_walkthrough(client, today(cfg))
    settings = client.get("/settings").text
    # The theme choice comes first in Settings, in plain words.
    assert settings.index("Light or dark</h2>") < settings.index("<h2>Care team</h2>")
    for value, label in (("auto", "Automatic"), ("light", "Light"), ("dark", "Dark")):
        assert f'value="{value}" data-theme-pick>{label}</label>' in settings
    # Automatic follows the device, which already handles night. No separate timed or "Night" option.
    assert "Night" not in settings
    # Desktop sidebar has a one-tap switch. It stays hidden until the script can run it.
    home = client.get("/").text
    assert re.search(r"<button[^>]*data-theme-toggle hidden", home)
    # The saved choice is applied before the page paints, so there is no flash of the wrong theme.
    assert re.search(r'<script src="/static/js/theme\.js\?v=\w+"></script>', home)  # blocking, not deferred
    assert home.index("/static/js/theme.js") < home.index("<body>")


def test_asset_urls_change_with_content(client):
    # Cache-busting uses a content hash, so an update reaches phones that cached the old CSS.
    page = client.get("/settings").text
    m = re.search(r'/static/css/app\.css\?v=([0-9a-f]{10})"', page)
    assert m and f"/static/js/app.js?v={m.group(1)}" in page
