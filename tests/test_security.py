"""Protections for a private-network app: cross-site posts, unknown host names, headers, password guessing."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from chemotracker import security
from chemotracker.config import _hosts
from chemotracker.web import create_app

from .conftest import setup_walkthrough, today


def test_changes_from_another_site_are_blocked(client, cfg):
    setup_walkthrough(client, today(cfg))
    data = {"symptom": "nausea", "severity": "1"}
    r = client.post("/symptoms/new", data=data, headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and "another site" in r.text
    r = client.post("/symptoms/new", data=data, headers={"Origin": "null"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/symptoms/new", data=data, headers={"Referer": "https://evil.example/page"}, follow_redirects=False)
    assert r.status_code == 403
    # The app's own pages, with or without a port, and behind a reverse proxy.
    assert client.post("/symptoms/new", data=data, headers={"Origin": "http://testserver"},
                       follow_redirects=False).status_code == 303
    assert client.post("/symptoms/new", data=data, headers={"Host": "192.168.1.20:8000", "Origin": "http://192.168.1.20:8000"},
                       follow_redirects=False).status_code == 303
    assert client.post("/symptoms/new", data=data, headers={"Host": "chemo.example.com", "Origin": "https://chemo.example.com"},
                       follow_redirects=False).status_code == 303
    assert client.post("/symptoms/new", data=data, headers={"Host": "10.0.0.5:8000", "X-Forwarded-Host": "chemo.example.com",
                                                            "Origin": "https://chemo.example.com"},
                       follow_redirects=False).status_code == 303
    # Reading pages is never blocked by the check.
    assert client.get("/symptoms", headers={"Origin": "https://evil.example"}).status_code == 200


def test_any_host_name_works_by_default(client, cfg):
    setup_walkthrough(client, today(cfg))
    for host in ("chemo.example.com", "192.168.1.20:8000", "my-mac.tail1234.ts.net", "nas"):
        assert client.get("/", headers={"Host": host}).status_code == 200


def test_allowed_hosts_when_set(cfg, monkeypatch):
    assert _hosts("Chemo.Example.com, 192.168.1.20:8000 ,[::1]:8000") == ("chemo.example.com", "192.168.1.20", "::1")
    assert _hosts("chemo.example.com, *") == ("*",)
    locked = replace(cfg, allowed_hosts=("chemo.example.com", "192.168.1.20"))
    with TestClient(create_app(locked, start_scheduler=False)) as c:
        assert c.get("/healthz", headers={"Host": "chemo.example.com"}).status_code == 200
        assert c.get("/healthz", headers={"Host": "192.168.1.20:8000"}).status_code == 200
        assert c.get("/healthz", headers={"Host": "127.0.0.1:8000"}).status_code == 200  # Docker health check
        r = c.get("/healthz", headers={"Host": "rebind.evil.example"})
        assert r.status_code == 400 and "CHEMO_ALLOWED_HOSTS" in r.text


def test_security_headers(client, cfg):
    setup_walkthrough(client, today(cfg))
    for path in ("/", "/static/css/app.css", "/healthz"):
        h = client.get(path).headers
        assert "script-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    page = client.get("/settings").text
    assert "<script>" not in page and " on" + "click=" not in page  # nothing inline for the policy to allow
    assert client.get("/").headers["cache-control"] == "no-store"


def test_password_guessing_is_slowed_down(cfg):
    locked = replace(cfg, password="family")
    with TestClient(create_app(locked, start_scheduler=False)) as c:
        for _ in range(5):
            assert c.post("/login", data={"password": "nope", "next": "/"}).status_code == 401
        r = c.post("/login", data={"password": "family", "next": "/"})
        assert r.status_code == 429 and "Wait 15 minutes" in r.text  # even the right one waits
    t = security.LoginThrottle(tries=2, window=60)
    t.failed("a"); t.failed("a")
    assert t.blocked("a") and not t.blocked("b")
    t.succeeded("a")
    assert not t.blocked("a")
