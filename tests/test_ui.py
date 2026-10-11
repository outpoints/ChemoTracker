"""Checks on the stylesheet and templates that a browser-free test can make."""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "src" / "chemotracker" / "static" / "css" / "app.css"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "chemotracker" / "templates"


def _block(css: str, selector: str) -> dict[str, str]:
    start = css.index(selector)
    body = css[css.index("{", start) + 1:]
    if selector.startswith("@media"):
        body = body[body.index("{") + 1:]
    body = body[:body.index("}")]
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body))


def test_dark_colours_match_in_both_places():
    # Dark values are declared for "chosen dark" and again for "automatic dark". They must not drift apart.
    css = CSS.read_text()
    chosen = _block(css, ':root[data-theme="dark"] {\n')
    auto = _block(css, "@media (prefers-color-scheme: dark) {\n  :root[data-theme=\"auto\"]")
    assert chosen and chosen == auto


def test_no_labels_above_headings():
    # The design avoids small "eyebrow" labels stacked above page headings.
    offenders = [p.name for p in TEMPLATES.glob("*.html") if 'class="eyebrow' in p.read_text()]
    assert offenders == []


def test_stylesheet_has_no_stray_comment_text():
    # A comment missing its "/*" turns its text into a selector, and the browser silently drops the rule after it.
    css = CSS.read_text()
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "*/" not in bare and "/*" not in bare
    assert bare.count("{") == bare.count("}")
    for rule in re.findall(r"(?:^|})\s*([^{}@]+){", bare):
        # Selectors never have a word followed by ". " or ": " (that is sentence punctuation).
        assert not re.search(r"[A-Za-z]{2,}[.:;] [A-Za-z]", rule), f"prose in a selector: {rule[:60]!r}"


def test_reduced_motion_keeps_colour_fades_but_not_movement():
    css = CSS.read_text()
    block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    block = block[:block.index("\n}\n")]
    assert "animation: none" in block and "transition-property: color, background-color" in block
    assert "transform: none" in block


def test_browser_bar_colour_matches_the_canvas():
    css = CSS.read_text()
    light = _block(css, ":root {")["--canvas"].strip()
    dark = _block(css, ':root[data-theme="dark"] {\n')["--canvas"].strip()
    base = (TEMPLATES / "base.html").read_text()
    assert f'content="{light}" media="(prefers-color-scheme: light)"' in base
    assert f'content="{dark}" media="(prefers-color-scheme: dark)"' in base
