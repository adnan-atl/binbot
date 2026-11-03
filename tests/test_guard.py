"""Abuse protection on sign-up (per-IP rate limits, Turnstile human check) and the reminder
heartbeat that alerts when reminders stop."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core import guard
from app.core.config import get_settings
from app.models import Reminder
from tests.test_flow import signup


@pytest.fixture
def limits_on(monkeypatch):
    monkeypatch.setenv("RATE_LIMITS", "true")
    get_settings.cache_clear()
    guard.reset_limits()
    yield
    guard.reset_limits()
    get_settings.cache_clear()


@pytest.fixture
def human_on(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET", "test-secret")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "test-site")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_address_lookups_are_limited_per_ip(client, limits_on):
    for _ in range(10):
        assert client.get("/api/postcode/E8 3PB?addresses=true").status_code == 200
    r = client.get("/api/postcode/E8 3PB?addresses=true")
    assert r.status_code == 429 and r.json()["detail"] == guard.TOO_MANY
    assert client.get("/api/postcode/E8 3PB").status_code == 200  # the free lookup has its own, larger limit


def test_signups_are_limited_per_ip(client, limits_on):
    for _ in range(8):
        signup(client)
    r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "house_number": "12"})
    assert r.status_code == 429


def test_limits_off_by_setting(client):
    for _ in range(12):
        assert client.get("/api/postcode/E8 3PB?addresses=true").status_code == 200


def test_signup_needs_a_human_pass_when_turnstile_is_on(client, human_on, monkeypatch):
    body = {"postcode": "E8 3PB", "house_number": "12"}
    assert client.post("/api/signup/start", json=body).status_code == 403
    assert client.get("/api/postcode/E8 3PB?addresses=true").status_code == 403
    assert client.get("/api/postcode/E8 3PB").status_code == 200  # no pass needed to look a postcode up

    monkeypatch.setattr(guard, "verify_turnstile", lambda token, ip: False)
    assert client.post("/api/human", json={"token": "bad"}).status_code == 403
    monkeypatch.setattr(guard, "verify_turnstile", lambda token, ip: token == "good")
    from app.api import routes
    monkeypatch.setattr(routes, "verify_turnstile", guard.verify_turnstile)
    p = client.post("/api/human", json={"token": "good"}).json()["pass"]
    assert client.post("/api/signup/start", json=body, headers={"X-Human-Pass": p}).status_code == 200
    assert client.post("/api/signup/start", json=body, headers={"X-Human-Pass": p + "x"}).status_code == 403


def test_pass_expires_and_cannot_be_forged(human_on):
    p = guard.issue_pass(now=1_000_000)
    assert guard.pass_ok(p, now=1_000_000 + 60)
    assert not guard.pass_ok(p, now=1_000_000 + guard.PASS_SECONDS + 1)
    exp = p.split(".")[0]
    assert not guard.pass_ok(f"{exp}.{'0' * 32}", now=1_000_000)
    assert not guard.pass_ok("nonsense")


def test_no_turnstile_keys_means_no_check(client):
    assert client.post("/api/human", json={"token": "anything"}).json() == {"pass": ""}
    signup(client)  # no header needed


# ---- heartbeat ----------------------------------------------------------------------------------

def test_heartbeat_pings_after_each_run_and_flags_failed_runs(client, db, monkeypatch):
    from app.workers import monitor
    pings = []
    monkeypatch.setenv("HEARTBEAT_URL", "https://hc.example/ping/abc")
    get_settings.cache_clear()
    monkeypatch.setattr(monitor.httpx, "get", lambda url, timeout: pings.append(url))
    monitor.dispatch(db)  # nothing due: still a good run
    assert pings == ["https://hc.example/ping/abc"]

    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123"})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    first = db.scalar(select(Reminder).order_by(Reminder.send_at))
    from app.providers import messaging

    class Broken:
        name = "meta"

        def send(self, to, body):
            return messaging.SendResult(ok=False, error="token expired")
    monkeypatch.setattr(messaging, "get_provider", lambda: Broken())
    from app.services import reminders
    real = reminders.dispatch_due
    monkeypatch.setattr(monitor, "dispatch_due", lambda d: real(d, now_utc=first.send_at + timedelta(minutes=1)))
    assert monitor.dispatch(db) == 0
    assert pings[-1] == "https://hc.example/ping/abc/fail"
    get_settings.cache_clear()


def test_no_heartbeat_url_means_no_ping(db, monkeypatch):
    from app.workers import monitor
    monkeypatch.setattr(monitor.httpx, "get", lambda *a, **k: pytest.fail("pinged"))
    monitor.dispatch(db)


def test_index_gets_turnstile_key(monkeypatch):
    from app.main import render_index
    from app.core.config import get_settings
    assert 'var TS_KEY = "";' in render_index()
    monkeypatch.setattr(get_settings(), "turnstile_site_key", "0x4AAA</script>")
    page = render_index()
    assert "{{TURNSTILE" not in page
    assert 'var TS_KEY = "0x4AAA\\u003c/script>";' in page
