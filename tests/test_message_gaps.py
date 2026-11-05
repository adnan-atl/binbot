"""Edge cases found in the logic review: repeated taps, every way of saying stop, mixed
messages, bins that aren't reminded, and a paused account saying the sign-up hi."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Reminder, ReminderStatus, User, UserStatus
from app.services.reminders import dispatch_due
from tests.test_flow import signup

P = "+447700900123"


def say(c, t):
    return c.post("/api/inbound", json={"phone": P, "text": t}).json()["reply"]


@pytest.fixture
def uid(client, db):
    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123", "consent": True})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    first = db.scalar(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at))
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    return uid


def test_snooze_twice_then_done_cancels_the_nudge(client, db, uid):
    assert say(client, "SNOOZE").startswith("No problem, I'll nudge you again")
    assert say(client, "SNOOZE") == "Already snoozed: I'll nudge you again soon."
    assert say(client, "DONE").startswith("Nice one.")
    db.expire_all()
    assert not db.scalars(select(Reminder).where(
        Reminder.user_id == uid, Reminder.status == ReminderStatus.scheduled,
        Reminder.send_at <= db.scalar(select(Reminder.send_at).where(Reminder.user_id == uid)
                                      .order_by(Reminder.send_at)) + timedelta(days=1))).all()


def test_wrong_after_snooze_still_counts(client, db, uid):
    say(client, "SNOOZE")
    assert say(client, "WRONG").startswith("Thanks, noted.")


@pytest.mark.parametrize("t", ["unsubscribe", "please stop", "stop please", "Stop messaging me",
                               "opt out", "STOP reminders", "cancel"])
def test_every_way_of_saying_stop_pauses(client, db, uid, t):
    assert say(client, t).startswith("Reminders are off.")
    db.expire_all()
    assert db.get(User, uid).status == UserStatus.stopped


def test_mixed_message_gets_the_useful_reply(client, db, uid):
    assert say(client, "thanks but my bin wasn't collected").startswith("Sorry about that.")


def test_turned_off_and_daily_bins(client, db, uid):
    say(client, "turn off garden")
    assert say(client, "when is garden collected?").startswith(
        "You've turned off reminders for Green garden waste bin.")


def test_paused_account_saying_the_hi_turns_back_on(client, db, uid):
    say(client, "STOP")
    assert say(client, "Hey Binny, I'M BIN").startswith("Reminders are back on.")
