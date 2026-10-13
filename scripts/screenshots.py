"""Take README and setup-guide screenshots from made-up data only.

    # App screens, from a demo instance (scripts/demo_seed.py):
    uv run --with playwright python scripts/screenshots.py http://127.0.0.1:8770 docs/screenshots

    # First-run walkthrough, from a fresh instance with an empty data folder:
    uv run --with playwright python scripts/screenshots.py --walkthrough http://127.0.0.1:8772 docs/screenshots

The walkthrough types the same made-up people as the demo (John, Jane, Sam), an "Example" clinic and
555-01xx numbers. Never point either mode at a real household's instance.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

SHOTS = [
    # (file, path, device, theme, element to scroll to the top of the screen, or None)
    ("today-phone.png", "/", "phone", "light", None),
    ("today-phone-dark.png", "/", "phone", "dark", None),
    ("symptom-phone.png", "/symptoms/new?s=nausea", "phone", "light", None),
    ("symptom-saved-phone.png", "{nausea}/saved", "phone", "light", None),
    ("log-dose-phone.png", "/dose/new", "phone", "light", None),
    ("medicines-phone.png", "/medicines", "phone", "light", None),
    ("people-phone.png", "/who", "phone", "light", None),
    ("reminders-phone.png", "/settings", "phone", "light", "#reminders"),
    ("backups-phone.png", "/settings", "phone", "light", "#backups"),
    ("today-desktop.png", "/", "desktop", "light", None),
    ("today-desktop-dark.png", "/", "desktop", "dark", None),
    ("cycle-desktop.png", "/cycles/1", "desktop", "light", None),
    ("report-desktop.png", "/report", "desktop", "light", None),
]

# Typed into the walkthrough's "Write in your prescriptions" box. Example doses, not anyone's prescription.
RX_TEXT = (
    "Ondansetron 4 mg tablet - Take 1 tablet by mouth every 8 hours as needed for nausea\n"
    "Prochlorperazine 5 mg tablet - Take 1 tablet by mouth every 6 hours as needed for nausea"
)


def contexts(p):
    phone = p.devices["iPhone 15"]
    return {"phone": (p.webkit, {**phone, "device_scale_factor": 2}),
            "desktop": (p.chromium, {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1})}


def scroll_to(page: Page, selector: str, gap: int = 12) -> None:
    """Bring an element to the top of the screen, just below the sticky header (viewport shots only: full-page
    and element shots taller than the screen draw the fixed header and tab bar in the middle)."""
    page.locator(selector).first.evaluate(
        """(e, gap) => {
             const head = document.querySelector('.topbar, header');
             const top = head && getComputedStyle(head).position !== 'static' ? head.getBoundingClientRect().height : 0;
             window.scrollTo(0, window.scrollY + e.getBoundingClientRect().top - top - gap);
           }""", gap)
    page.wait_for_timeout(300)


def settle(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)  # let the reveal and countdown animations finish


def app_shots(base: str, out: Path) -> None:
    with sync_playwright() as p:
        engines = contexts(p)
        for name, path, device, theme, element in SHOTS:
            engine, opts = engines[device]
            browser = engine.launch()
            page = browser.new_context(**opts, color_scheme=theme).new_page()
            page.goto(base + "/", wait_until="networkidle")
            if "/who" in page.url and path != "/who":
                page.locator("button.who-btn").first.click()  # pick John on a fresh device
            if path.startswith("{nausea}"):
                page.goto(base + "/symptoms", wait_until="networkidle")
                href = page.locator('a[aria-label^="Edit Nausea"]').first.get_attribute("href")
                path = path.replace("{nausea}", href)
            page.goto(base + path, wait_until="networkidle")
            settle(page)
            if element:
                scroll_to(page, element)
            page.screenshot(path=str(out / name))
            print(out / name)
            browser.close()


def walkthrough(base: str, out: Path) -> None:
    """Fill in the first-run walkthrough on an empty instance, one screenshot per step."""
    with sync_playwright() as p:
        engine, opts = contexts(p)["phone"]
        browser = engine.launch()
        page = browser.new_context(**opts, color_scheme="light", reduced_motion="reduce").new_page()

        def shot(name: str, at: str | None = None) -> None:
            if at:
                scroll_to(page, at)
            else:
                page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)
            page.screenshot(path=str(out / name))
            print(out / name)

        page.goto(base + "/setup", wait_until="networkidle")
        if not page.url.rstrip("/").endswith("/setup"):
            sys.exit("This instance is already set up. Point --walkthrough at one with an empty data folder.")
        shot("setup-1-welcome.png")

        page.goto(base + "/setup/people", wait_until="networkidle")
        page.locator('input[name="names[]"]').nth(0).fill("John")
        for name, role in (("Jane", "family"), ("Sam", "caregiver")):
            page.locator("[data-add-person]").click()
            row = page.locator("[data-person-row]").last
            row.locator('input[name="names[]"]').fill(name)
            row.locator(f'input[value="{role}"]').check()
        shot("setup-2-people.png")
        page.locator('button[type="submit"]').click()
        settle(page)

        day1 = date.today() - timedelta(days=1)
        page.locator("#day1").fill(day1.isoformat())
        page.locator("#count").fill("4")
        page.locator("#regimen").fill("Example: AC")
        shot("setup-3-chemo-day.png")
        page.locator('button[type="submit"]').click()
        settle(page)

        page.locator("#rx_text").fill(RX_TEXT)
        page.locator("[data-rx-preview] *").first.wait_for()
        settle(page)
        shot("setup-4-medicines.png", "#rx_text")
        page.locator('.setup-nav button[type="submit"]').click()
        settle(page)

        shot("setup-5-label.png")
        while "/setup/label/" in page.url:
            page.locator('.setup-nav button[type="submit"]').click()
            settle(page)

        page.locator('input[name="clinic_name"]').fill("Example Cancer Center")
        page.locator('input[name="clinic_phone"]').fill("555-0100")
        page.locator('input[name="after_hours_phone"]').fill("555-0199")
        shot("setup-6-care-team.png")
        page.locator('.setup-nav button[type="submit"]').click()
        settle(page)
        shot("setup-7-today.png")
        browser.close()


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--walkthrough"]
    base, out = args[0].rstrip("/"), Path(args[1])
    out.mkdir(parents=True, exist_ok=True)
    (walkthrough if "--walkthrough" in sys.argv else app_shots)(base, out)


if __name__ == "__main__":
    main()
