"""Genuine questions about their collections get answered from their own calendar."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import ChangeLog, Reminder
from app.services import reminders
from tests.test_flow import signup

PHONE = "+447700900123"


@pytest.fixture
def uid(client):
    uid = signup(client)["user_id"]  # demo round: Tuesdays
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123", "consent": True})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


def say(client, text):
    return client.post("/api/inbound", json={"phone": PHONE, "text": text}).json()["reply"]


def _first(db, uid):
    db.expire_all()
    return db.scalar(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at))


def test_next_collection(client, db, uid):
    r = _first(db, uid)
    names = [b["label"] for b in r.bins]
    day = r.collection_date.strftime("%a %-d %b")
    for q in ("When's my next collection?", "when is my next bin day", "what bin goes out next",
              "Which bins next?"):
        reply = say(client, q)
        assert reply.startswith("Next up: ") and day in reply, q
        assert all(n in reply for n in names), q
        assert reply.endswith("I'll remind you at 7pm the evening before."), q


def test_a_particular_bin(client, db, uid):
    reply = say(client, "when is garden waste collected?")
    assert reply.startswith("Green garden waste bin: next out on ")
    assert say(client, "when's the glass going?").startswith("I don't have that bin on your calendar.")


def test_tomorrow_and_this_week(client, db, uid, monkeypatch):
    r = _first(db, uid)
    monday = r.collection_date - timedelta(days=1)  # collections are on Tuesdays

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(monday, datetime.min.time(), tz).replace(hour=12)

    monkeypatch.setattr(reminders, "datetime", FakeDT)
    assert say(client, "what's going out tomorrow?").startswith(
        f"Tomorrow ({r.collection_date.strftime('%a %-d %b')}): ")
    assert say(client, "which bins this week").startswith("This week: ")


def test_not_questions_still_go_where_they_did(client, db, uid):
    assert not say(client, "my bin wasn't collected today").startswith("Next up")
    assert not say(client, "when is the christmas collection?").startswith("Next up")
    assert not say(client, "do you do bulky waste?").startswith("Next up")
    db.expire_all()
    assert len(db.scalars(select(ChangeLog).where(ChangeLog.kind == "unrecognised")).all()) == 3
