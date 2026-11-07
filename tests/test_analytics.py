"""Product analytics (services/analytics.py, docs/analytics.md): off without POSTHOG_KEY; with it,
the server records what residents do under their user id, and never their phone number,
postcode, address or what they typed. The website gets PostHog only on the oat design."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Reminder
from app.services import analytics
from app.services.reminders import dispatch_due
from tests.test_flow import signup

PHONE = "+447700900123"


@pytest.fixture
def events(monkeypatch):
    """Analytics on, with PostHog swapped for a list of (event, distinct_id, properties)."""
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    get_settings.cache_clear()
    got = []
    monkeypatch.setattr(analytics, "_capture", lambda e, d, p: got.append((e, d, p)))
    return got


def names(events):
    return [e for e, _, _ in events]


def switch_on(client):
    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123"})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


def say(client, text):
    return client.post("/api/inbound", json={"phone": PHONE, "text": text}).json()["reply"]


def test_off_without_a_key(client, monkeypatch):
    got = []
    monkeypatch.setattr(analytics, "_capture", lambda e, d, p: got.append(e))
    switch_on(client)
    say(client, "STOP")
    assert got == [] and not analytics.enabled()


def test_a_residents_journey(client, db, events):
    uid = switch_on(client)
    first = db.scalar(select(Reminder).order_by(Reminder.send_at))
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    assert "Nice one" in say(client, "done")
    say(client, "menu:settings")
    say(client, "STOP")
    say(client, "START")
    say(client, "what is the meaning of life")

    assert names(events) == ["signup_started", "reminders_switched_on", "reminder_sent",
                             "reminder_replied", "menu_tapped", "reminders_paused",
                             "reminders_resumed", "message_not_understood"]
    assert {d for _, d, _ in events} == {uid}  # one person, by their user id
    by = {e: p for e, _, p in events}
    assert by["signup_started"]["via"] == "website" and by["signup_started"]["council"] == "Hackney"
    assert by["reminders_switched_on"]["mode"] == "night_before"
    assert by["reminder_replied"]["reply"] == "done"
    assert by["reminder_sent"]["bins"] >= 1


def test_never_sends_personal_details(client, db, events):
    switch_on(client)
    say(client, "my number is 07700 900123 and I live at E8 3PB, 12 Mare Street")
    sent = repr(events)
    for secret in ("07700", "900123", "+447700", "E8 3PB", "e83pb", "Mare Street"):
        assert secret.lower() not in sent.lower()


def test_a_posthog_failure_never_breaks_a_reply(client, monkeypatch):
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    get_settings.cache_clear()

    def boom(*a):
        raise RuntimeError("PostHog is down")

    monkeypatch.setattr(analytics, "_capture", boom)
    switch_on(client)
    assert "off" in say(client, "STOP")


def test_whatsapp_sign_up_is_tracked(client, events):
    say(client, "hello")
    assert names(events) == ["signup_started"] and events[0][2]["via"] == "whatsapp"


def test_website_gets_posthog_only_on_oat(client, monkeypatch):
    from app.main import WEB, render_index

    assert '<script>' in render_index(WEB / "designs" / "oat.html")
    assert "var PH = null;" in render_index(WEB / "designs" / "oat.html")  # off without a key
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    get_settings.cache_clear()
    oat = render_index(WEB / "designs" / "oat.html")
    assert '"key": "phc_test"' in oat and '"host": "/ingest"' in oat
    assert '"ui": "https://eu.posthog.com"' in oat
    assert "persistence: 'memory'" in oat and "autocapture: false" in oat
    for other in ("og", "silly"):  # the original page stays as it was
        assert "posthog" not in render_index(WEB / "designs" / f"{other}.html").lower()


def test_caddy_passes_ingest_on_to_posthog():
    from pathlib import Path

    caddy = (Path(__file__).parent.parent / "deploy" / "Caddyfile").read_text()
    assert "handle_path /ingest/static/*" in caddy and "eu-assets.i.posthog.com" in caddy
    assert "handle_path /ingest/* {" in caddy and "header_up Host eu.i.posthog.com" in caddy
    assert caddy.count("{") == caddy.count("}")
    # the site itself is still served, inside a handle so /ingest never reaches it
    assert "handle {\n\t\treverse_proxy api:8000" in caddy


def test_hi_from_a_number_nobody_typed(client, events):
    from app.services.reminders import hi_text

    say(client, hi_text())
    assert names(events) == ["signup_started", "hi_from_unexpected_number"]
