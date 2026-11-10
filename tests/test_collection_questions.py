"""Genuine questions about their collections get answered from their own calendar."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import ChangeLog, Reminder
from app.services import reminders
from tests.test_flow import signup

PHONE = "+447700900123"


@pytest.fixture
def uid(client, midday):
    uid = signup(client)["user_id"]  # demo round: Tuesdays
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123", "consent": True})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


def say(client, text):
    return client.post("/api/inbound", json={"phone": PHONE, "text": text}).json()["reply"]


def _first(db, uid):
    db.expire_all()
    return db.scalar(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at))


def test_next_collection(client, db, uid, midday):
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
    assert reply.startswith("\U0001F7E2 Green garden waste bin: next out on ")
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


@pytest.mark.parametrize("q,starts,team", [
    ("do you do bulky waste? got an old sofa", "Bulky items are collected by Hackney", True),
    ("my bin wasn't collected today", "Sorry about that. I can't report missed collections, but Hackney can", True),
    ("can I recycle pizza boxes?", "I don't know Hackney's rules for every item", True),
    ("when is the christmas collection?", "When Hackney moves collections for a bank holiday", True),
    ("my bin is broken", "New and replacement bins come from Hackney", True),
    ("where's the nearest tip", "Tips and recycling centres are run by Hackney", True),
    ("what's the forecast for tomorrow?", "I'm a bin butler, not a weatherman!", False),
    ("can you help with my energy bills", "That's beyond my bin grade, sorry.", True),
    ("thanks for the reminder!", "You're welcome!", False),
    ("hello", "Hi! I'll message you the evening before", False),
    ("who are you?", "I'm Binny, by Alfred.", False),
])
def test_tailored_cant_help_replies(client, db, uid, q, starts, team):
    assert say(client, q).startswith(starts)
    db.expire_all()
    from app.models import ChangeLog
    seen = db.scalars(select(ChangeLog).where(ChangeLog.kind == "unrecognised")).all()
    assert bool(seen)  # always kept for review; the Slack inbox only gets the ones marked team


@pytest.mark.parametrize("q", ["Remind me of my dates?", "what's my schedule", "my bin dates please",
                               "what's coming up?", "which days are my collections"])
def test_asking_for_the_dates_lists_the_next_few(client, db, uid, q):
    reply = say(client, q)
    assert reply.startswith("Your next collections:\n"), q
    assert reply.count("\n") >= 3 and reply.endswith("I'll remind you at 7pm the evening before."), q


def test_bare_time_questions(client, db, uid, midday):
    """"What is tomorrow?" (Hounslow, 10 Nov 2026) has no bin word in it, but sent to a bin bot
    it's a question about bins: answer it from their calendar, don't hand it to the team."""
    for q in ("What is tomorrow?", "whats tomorrow", "tomorrow?", "What’s out tonight?",
              "what's on tomorrow"):
        reply = say(client, q)
        assert reply.startswith(("Tomorrow (", "Nothing tomorrow.")), q
    for q in ("anything this week?", "what about this week"):
        assert say(client, q).startswith(("This week:", "Nothing more this week.")), q
    assert say(client, "what about next week?").startswith(("Next week:", "Nothing next week."))
    assert say(client, "what's today?").startswith(("Today (", "Nothing today."))


def test_a_sentence_with_tomorrow_in_it_is_not_a_question(client, db, uid, midday):
    for q in ("I am away tomorrow", "tomorrow I will be late"):
        reply = say(client, q)
        assert not reply.startswith(("Tomorrow (", "Nothing tomorrow.")), q


def test_happy_sign_offs_get_youre_welcome(client, db, uid, midday):
    """"All good" (Kensington and Chelsea, 10 Nov 2026), "Great", "Perfect, thanks!": a happy
    sign-off gets a "You're welcome"; a plain "ok" gets nothing; sarcasm gets neither."""
    for t in ("All good", "all good!", "Perfect", "Great \U0001F44D", "That's great", "Lovely thanks",
              "Perfect, thanks so much!", "thanks"):
        assert say(client, t).startswith("You're welcome"), t
    for t in ("ok", "got it", "\U0001F44D"):
        assert say(client, t) == "", t
    for t in ("great, now I have to wait a week", "thanks but it's still wrong",
              "great, when is my next collection?"):
        assert not say(client, t).startswith("You're welcome"), t
