"""The one "share with neighbours" message: after the first Done, or the day after the first
collection; only for people who didn't opt out of hearing from us."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.config import get_settings
from app.models import Outbox, Reminder, ReminderStatus, User
from app.services.reminders import dispatch_due, share_nudges
from tests.test_flow import signup

PHONE = "+447700900123"
LONDON = ZoneInfo("Europe/London")


def _on(client, marketing=True):
    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123", "consent": True,
                                                 "marketing_consent": marketing})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


def _send_first(db, uid):
    db.expire_all()
    first = db.scalar(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at))
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    return first


def _shares(db):
    db.expire_all()
    return db.scalars(select(Outbox).where(Outbox.kind == "share")).all()


def _say(client, text):
    return client.post("/api/inbound", json={"phone": PHONE, "text": text}).json()["reply"]


def test_done_on_the_first_reminder_sends_the_share_link_once(client, db):
    uid = _on(client)
    _send_first(db, uid)
    _say(client, "DONE")
    [msg] = _shares(db)
    code = db.get(User, uid).referral_code
    assert msg.body == ("If you want to share Binny with neighbours or friends, send them this "
                        f"link: http://localhost:8000/?ref={code}")
    assert db.get(User, uid).share_nudged_at
    _say(client, "DONE")
    assert len(_shares(db)) == 1


def test_day_after_the_first_collection_if_they_never_tapped_done(client, db):
    uid = _on(client)
    first = _send_first(db, uid)
    same_day = datetime.combine(first.collection_date, time(11, 7), LONDON)
    assert share_nudges(db, now_utc=same_day) == 0  # collection not over yet
    next_day = same_day + timedelta(days=1)
    assert share_nudges(db, now_utc=next_day) == 1
    [msg] = _shares(db)
    assert msg.body.startswith("Hope that made bin day easier. If you want to share Binny")
    assert share_nudges(db, now_utc=next_day + timedelta(days=1)) == 0


def test_no_nudge_for_people_who_opted_out(client, db):
    uid = _on(client, marketing=False)
    first = _send_first(db, uid)
    _say(client, "DONE")
    nxt = datetime.combine(first.collection_date + timedelta(days=1), time(11, 7), LONDON)
    assert share_nudges(db, now_utc=nxt) == 0
    assert _shares(db) == []


def test_no_nudge_before_any_reminder_went_out(client, db):
    uid = _on(client)
    db.expire_all()
    first = db.scalar(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at))
    assert first.status == ReminderStatus.scheduled
    later = datetime.combine(first.collection_date + timedelta(days=3), time(11, 7), LONDON)
    # its reminder time has passed but the worker never sent it: no nudge about it
    assert share_nudges(db, now_utc=later) == 0


def test_real_whatsapp_waits_for_the_approved_template(client, db, monkeypatch):
    from app.providers import messaging

    class FakeMeta(messaging.StubProvider):
        name = "meta"

        def send_template(self, to_phone, name, lang, body_params, buttons=False):
            self.sent = (name, body_params)
            return messaging.SendResult(ok=True, message_id="t")

    fake = FakeMeta()
    uid = _on(client)
    first = _send_first(db, uid)
    monkeypatch.setattr(messaging, "get_provider", lambda: fake)
    monkeypatch.setattr("app.services.reminders.get_provider", lambda: fake)
    nxt = datetime.combine(first.collection_date + timedelta(days=1), time(11, 7), LONDON)
    assert share_nudges(db, now_utc=nxt) == 0  # no template yet: nothing sent, still queued
    monkeypatch.setenv("META_SHARE_TEMPLATE", "bin_share")
    get_settings.cache_clear()
    assert share_nudges(db, now_utc=nxt) == 1
    code = db.get(User, uid).referral_code
    assert fake.sent == ("bin_share", ["Binny", f"http://localhost:8000/?ref={code}"])


def test_done_tapped_twice_just_says_already_sorted(client, db):
    from app.models import ChangeLog, Reminder
    from app.services.reminders import dispatch_due

    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123", "consent": True})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    first = db.scalar(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at))
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    assert _say(client, "DONE").startswith("Nice one.")
    assert _say(client, "DONE") == "Already sorted \U0001F44D I'll be in touch before the next one."
    db.expire_all()
    assert not db.scalars(select(ChangeLog).where(ChangeLog.kind == "unrecognised")).all()
