"""Take README screenshots from a running demo instance (see scripts/demo_seed.py).

    uv run --with playwright python scripts/screenshots.py http://127.0.0.1:8770 docs/screenshots

Uses only the fictional demo data. Never point it at a real household's instance.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOTS = [
    # (file, path, device, theme, full_page)
    ("today-phone.png", "/", "phone", "light", False),
    ("today-phone-night.png", "/", "phone", "dark", False),
    ("symptom-phone.png", "/symptoms/new?s=nausea", "phone", "light", False),
    ("symptom-saved-phone.png", "/symptoms/6/saved", "phone", "light", False),
    ("today-desktop.png", "/", "desktop", "light", False),
    ("cycle-desktop.png", "/cycles/1", "desktop", "light", False),
    ("people-phone.png", "/who", "phone", "light", False),
    ("setup-phone.png", "/setup/medicines", "phone", "light", False),
]


def main() -> None:
    base, out = sys.argv[1].rstrip("/"), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        phone = p.devices["iPhone 15"]
        engines = {"phone": (p.webkit, {**phone, "device_scale_factor": 2}),
                   "desktop": (p.chromium, {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1})}
        for name, path, device, theme, full in SHOTS:
            engine, opts = engines[device]
            browser = engine.launch()
            ctx = browser.new_context(**opts, color_scheme=theme)
            page = ctx.new_page()
            page.goto(base + path, wait_until="networkidle")
            if page.url.split(base, 1)[-1].startswith("/who") and not path.startswith("/who"):
                page.locator("button.who-btn").first.click()  # pick John on a fresh device
                page.goto(base + path, wait_until="networkidle")
            page.screenshot(path=str(out / name), full_page=full)
            print(out / name)
            browser.close()


if __name__ == "__main__":
    main()
