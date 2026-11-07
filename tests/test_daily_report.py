"""The nightly growth report (services/daily_report.py): the day's numbers from the database,
plus PostHog's visits and funnel when it can be read, posted to #binjamin-analytics."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Council, CouncilStatus, Reminder
from app.services import daily_report as R
from app.services.reminders import dispatch_due
from tests.test_flow import signup

LONDON = R.LONDON
TODAY = datetime.now(LONDON).date()


def switch_on(client, phone, ref=None, offers=True):
    uid = signup(client, ref=ref)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": phone, "consent": True,
                                                  "marketing_consent": offers})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


@pytest.fixture
def day(client, db):
    """Two residents switched on today, one from the other's invite link; a reminder sent and
    replied to."""
    uid = switch_on(client, "07700900123")
    code = client.get(f"/api/users/{uid}/referral").json()["code"]
    client.get(f"/r/{code}", follow_redirects=False, headers={"user-agent": "Mozilla/5.0"})
    switch_on(client, "07700900456", ref=code, offers=False)
    first = db.scalar(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at))
    first.send_at = datetime.now(LONDON) - timedelta(minutes=5)
    db.commit()
    dispatch_due(db)
    client.post("/api/inbound", json={"phone": "+447700900123", "text": "done"})
    return uid


def test_window_is_a_london_day():
    start, end = R.window(datetime(2026, 7, 1).date())
    assert start.isoformat() == "2026-06-30T23:00:00+00:00" and end - start == timedelta(hours=24)
    start, end = R.window(datetime(2026, 10, 25).date())  # clocks go back: 25 hours
    assert end - start == timedelta(hours=25)


def test_database_numbers(day, db):
    d = R.db_day(db, *R.window(TODAY))
    assert d.started == 2 and d.started_invited == 1
    assert d.switched_on == 2 and d.switched_on_invited == 1
    assert d.invite_clicks == 1 and d.referrals == 1
    assert d.reminders_sent == 1 and d.reminders_replied == 1 and d.replies["done"] == 1
    assert d.councils["Hackney"]["lookups"] == 2
    assert d.consented == 2 and d.offers_out == 1 and d.on_offers_out == 1


def test_report_without_posthog(day, db):
    text = R.build(db, TODAY)
    assert "Sign-ups started: *2*" in text and "Switched on: *2*" in text
    assert "Invite link clicks: 1" in text and "invited switched on: 1" in text
    assert "From an invite today: 50%" in text
    assert "not enough data yet" in text  # K-factor needs 10 switch-ons
    assert "Not available: set POSTHOG_PERSONAL_API_KEY" in text
    assert "Replied to: 100% (Done 1)" in text
    assert "*Alfred offers*" in text and "Opted out: *1* of 2 who gave their number (*50%*)" in text
    assert "Of today's switch-ons: 1 can hear about Alfred, 1 opted out" in text
    assert "All active residents: *1* of 2 can hear about Alfred (*50%*)" in text
    assert "07700" not in text and "E8" not in text  # nobody's details


def test_report_with_posthog_and_problem_councils(day, db, monkeypatch):
    fake = {"events": {"reminders_paused": 2, "account_deleted": 1},
            "sessions": {"$pageview": 40, "screen_viewed:postcode": 20, "postcode_looked_up": 18,
                         "calendar_loaded": 15, "calendar_confirmed": 14, "phone_added": 10,
                         "say_hello_tapped": 8},
            "sources": [("direct", 25), ("invite link", 15)], "shares": {"WhatsApp": 3},
            "calendar_failed": {"Glasgow City": 2}, "via": {"website": 2},
            "hello": {"phone": [4, 3], "computer": [2, 0]}}
    monkeypatch.setattr(R, "posthog_day", lambda s, e: fake)
    glasgow = db.scalar(select(Council).where(Council.name == "Hackney"))
    glasgow.status = CouncilStatus.broken
    db.commit()
    text = R.build(db, TODAY)
    assert "Visits: *40*" in text and "visit → switched on: *5%*" in text
    assert "Postcode screen: 20 (50%)" in text
    assert "Biggest drop: *Postcode screen*" in text
    assert "invite link 15" in text and "WhatsApp 3" in text
    assert "Paused 2" in text and "deleted 1" in text
    assert "*Glasgow City*: 2 bin days not found on the site" in text
    assert "Health check failing: " + glasgow.name in text
    assert "Tapped say hello → switched on: computer 0/2 (0%), phone 3/4 (75%)" in text


def test_k_factor(client, db):
    for i in range(10):
        switch_on(client, f"0770090{i:04d}")
    k, n = R.k_factor(db, datetime.now(LONDON) + timedelta(minutes=1))
    assert n == 10 and k == 0.0


def test_posts_to_the_analytics_channel(day, db, monkeypatch):
    from app.core.config import get_settings
    from app.services import inbox

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    get_settings.cache_clear()
    calls = []

    def api(method, **body):
        calls.append((method, body))
        if method == "chat.postMessage" and body["channel"] == "binjamin-analytics":
            return {"ok": False, "error": "not_in_channel"}
        return {"ok": True}

    monkeypatch.setattr(inbox, "_api", api)
    monkeypatch.setattr(inbox, "_get", lambda m, **p: {"channels": [{"id": "C1", "name": "binjamin-analytics"}]})
    assert R.post(db, TODAY)
    assert [m for m, _ in calls] == ["chat.postMessage", "conversations.join", "chat.postMessage"]
    assert calls[-1][1]["channel"] == "C1" and "daily report" in calls[-1][1]["text"]


def test_off_without_slack(db):
    assert R.post(db, TODAY) is False
