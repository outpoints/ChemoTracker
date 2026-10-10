"""Protections for an app reached over a home network or Tailscale.

ChemoTracker is meant for a private network, never the open internet. These guard against
the ways a private web app is usually attacked from the outside anyway, through a family
member's browser:

- Cross-site form posts (CSRF): a page on another site can't make the browser change the
  record. Changes must come from ChemoTracker's own pages (the Origin or Referer matches).
- DNS rebinding: with CHEMO_ALLOWED_HOSTS set, only the listed names are answered.
  Off by default so any LAN name, IP address or Tailscale name works.
- Security headers: a Content Security Policy that only allows the app's own files, no framing,
  no sniffing, and no referrer to other sites.
- Password guessing (when CHEMO_PASSWORD is set): 5 wrong tries from one address in 15 minutes
  means a 15 minute wait.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ALWAYS_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}  # the container's own health check

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",  # style="" attributes in the templates; no outside styles
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "manifest-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "object-src 'none'",
])

HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def _netloc(value: str) -> str:
    """'https://Chemo.example:443' -> 'chemo.example'. Default ports are dropped so they compare equal."""
    parts = urlsplit(value if "//" in value else f"//{value}")
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is None or (parts.scheme, port) in (("http", 80), ("https", 443)) or (not parts.scheme and port in (80, 443)):
        return host
    return f"{host}:{port}"


def host_name(request: Request) -> str:
    """The name the browser used, without the port: 'chemo.example:8000' -> 'chemo.example'."""
    return (urlsplit("//" + request.headers.get("host", "")).hostname or "").lower()


def host_allowed(request: Request, allowed: tuple[str, ...]) -> bool:
    if not allowed or "*" in allowed:
        return True
    name = host_name(request)
    return name in ALWAYS_ALLOWED_HOSTS or name in allowed


def same_origin(request: Request) -> bool:
    """For changes: did this come from one of the app's own pages?"""
    source = request.headers.get("origin")
    if source is None:
        source = request.headers.get("referer")
        if source is None:
            return True  # not a browser (curl, scripts); browsers always send one on a form post
    if source == "null":
        return False
    here = {_netloc(request.headers.get("host", ""))}
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:  # a reverse proxy in front; a web page can't set this header on a form post
        here |= {_netloc(h.strip()) for h in forwarded.split(",")}
    return _netloc(source) in here


class LoginThrottle:
    def __init__(self, tries: int = 5, window: float = 15 * 60) -> None:
        self.tries, self.window = tries, window
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, who: str) -> deque[float]:
        q = self._fails[who]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, who: str) -> bool:
        return len(self._recent(who)) >= self.tries

    def failed(self, who: str) -> None:
        self._recent(who).append(time.monotonic())
        if len(self._fails) > 1000:  # forget the oldest addresses rather than grow without limit
            for key in list(self._fails)[:500]:
                self._fails.pop(key, None)

    def succeeded(self, who: str) -> None:
        self._fails.pop(who, None)


def refuse(status: int, title: str, body: str) -> Response:
    page = (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{title}</title><body style='font-family:system-ui;max-width:36rem;margin:3rem auto;padding:0 1rem;"
            f"line-height:1.5'><h1 style='font-size:1.4rem'>{title}</h1><p>{body}</p></body></html>")
    response = HTMLResponse(page, status_code=status)
    for k, v in HEADERS.items():
        response.headers[k] = v
    return response
