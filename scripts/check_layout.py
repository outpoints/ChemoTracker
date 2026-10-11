"""Check that no page scrolls sideways on small phones (demo data only).

Start a demo instance first (see scripts/demo_seed.py), then:

    uv run --with playwright python scripts/check_layout.py http://127.0.0.1:8770

Loads every main page at 320, 360, 375 and 393 px wide in WebKit and Chromium and fails if the
page is wider than the screen or a control pokes out of the screen. 320 px is what an iPhone
shows with Display Zoom turned on, which many people with low vision use.
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

WIDTHS = (320, 360, 375, 393)
ROUTES = ["/", "/symptoms", "/symptoms/new?s=nausea", "/medicines", "/medicines/add", "/cycles", "/report",
          "/report?start=2026-01-01&end=2026-01-31", "/settings", "/history", "/people", "/who", "/setup/medicines"]

# Elements that are allowed to scroll sideways on purpose.
SCROLLERS = ".day-strip, .sheet-wrap, .heat-wrap"

PROBE = """(scrollers) => {
  const vw = document.documentElement.clientWidth;
  const out = [];
  const inScroller = (el) => el.closest(scrollers);
  for (const el of document.querySelectorAll("body *")) {
    if (inScroller(el)) continue;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || cs.position === "fixed") continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.right > vw + 0.5 || r.left < -0.5) {
      if (el.closest(".visually-hidden, .skip-link")) continue;
      out.push(`${el.tagName.toLowerCase()}.${[...el.classList].join(".")} ${Math.round(r.left)}..${Math.round(r.right)} (screen ${vw})`);
    }
  }
  return {scroll: document.documentElement.scrollWidth, vw, out: out.slice(0, 5)};
}"""


def main() -> int:
    base = sys.argv[1].rstrip("/")
    problems = []
    with sync_playwright() as p:
        for engine in (p.webkit, p.chromium):
            browser = engine.launch()
            for width in WIDTHS:
                ctx = browser.new_context(viewport={"width": width, "height": 700}, device_scale_factor=2,
                                          is_mobile=engine is p.chromium, has_touch=True)
                page = ctx.new_page()
                page.goto(base + "/", wait_until="networkidle")
                if "/who" in page.url:
                    page.locator("button.who-btn").first.click()
                    page.wait_for_load_state("networkidle")
                for route in ROUTES:
                    page.goto(base + route, wait_until="networkidle")
                    res = page.evaluate(PROBE, SCROLLERS)
                    if res["scroll"] > res["vw"] or res["out"]:
                        problems.append(f"{engine.name} {width}px {route}: page {res['scroll']}px wide; " + "; ".join(res["out"]))
                ctx.close()
            browser.close()
    for line in problems:
        print(line)
    print(f"{len(problems)} problem(s)" if problems else "No sideways overflow at " + ", ".join(f"{w}px" for w in WIDTHS))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
