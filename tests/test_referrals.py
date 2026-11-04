"""Referrals count when the friend's reminders go on, and the referrer hears about them on their
next reminder (batched), never in a message of its own."""
from datetime import timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.models import Outbox, Referral, Reminder, ReminderStatus
from app.providers import messaging
from app.services.reminders import dispatch_due
from tests.test_flow import signup


def _activate(client, uid, phone):
    client.post(f"/api/users/{uid}/phone", json={"phone": phone})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})


def _invite(client, code, phone):
    fid = signup(client, ref=code)["user_id"]
    _activate(client, fid, phone)
    return fid


def _first_due(db, uid):
    db.expire_all()
    return db.scalar(select(Reminder).where(Reminder.user_id == uid,
                                            Reminder.status == ReminderStatus.scheduled)
                     .order_by(Reminder.send_at))


def test_unfinished_sign_up_does_not_count(client, db):
    uid = signup(client)["user_id"]
    _activate(client, uid, "07700900123")
    code = client.get(f"/api/users/{uid}/referral").json()["code"]
    signup(client, ref=code)  # never finishes
    assert client.get(f"/api/users/{uid}/referral").json()["referrals"] == 0


def test_news_is_batched_onto_the_next_reminder_and_sent_once(client, db):
    uid = signup(client)["user_id"]
    _activate(client, uid, "07700900123")
    code = client.get(f"/api/users/{uid}/referral").json()["code"]
    welcomes = db.scalar(select(Outbox).where(Outbox.to_phone == "+447700900123").order_by(Outbox.created_at.desc()))
    _invite(client, code, "07700900201")
    _invite(client, code, "07700900202")
    # nothing extra went to the referrer when the friends joined
    db.expire_all()
    assert db.scalar(select(Outbox).where(Outbox.to_phone == "+447700900123")
                     .order_by(Outbox.created_at.desc())).id == welcomes.id

    first = _first_due(db, uid)
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    msg = db.scalar(select(Outbox).where(Outbox.to_phone == "+447700900123", Outbox.kind == "reminder"))
    assert "2 neighbours joined with your link" in msg.body
    assert "You're now a Kerbside regular. 1 more to Street captain." in msg.body
    assert all(r.announced_at for r in db.scalars(select(Referral)))

    second = _first_due(db, uid)
    dispatch_due(db, now_utc=second.send_at + timedelta(minutes=1))
    later = db.scalars(select(Outbox).where(Outbox.to_phone == "+447700900123",
                                            Outbox.kind == "reminder")).all()
    assert len(later) == 2 and "joined with your link" not in later[-1].body


def test_with_a_plain_template_the_news_rides_on_the_done_reply(client, db, monkeypatch):
    monkeypatch.setattr(messaging.StubProvider, "send_template",
                        lambda self, *a, **k: messaging.SendResult(ok=True, message_id="t"),
                        raising=False)
    uid = signup(client)["user_id"]
    _activate(client, uid, "07700900123")
    code = client.get(f"/api/users/{uid}/referral").json()["code"]
    _invite(client, code, "07700900201")

    first = _first_due(db, uid)
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    assert db.scalar(select(Referral)).announced_at is None  # the template can't carry it

    reply = client.post("/api/inbound", json={"phone": "+447700900123", "text": "done"}).json()["reply"]
    assert "Nice one" in reply and "A neighbour joined with your link" in reply
    db.expire_all()
    assert db.scalar(select(Referral)).announced_at is not None
    reply = client.post("/api/inbound", json={"phone": "+447700900123", "text": "done"}).json()["reply"]
    assert "joined" not in reply


def test_news_template_carries_the_line_as_a_fourth_value(client, db, monkeypatch):
    sent = []
    monkeypatch.setattr(messaging.StubProvider, "send_template",
                        lambda self, to, name, lang, params, buttons: (sent.append((name, params)),
                                                                       messaging.SendResult(ok=True, message_id="t"))[1],
                        raising=False)
    monkeypatch.setenv("META_REMINDER_NEWS_TEMPLATE", "bin_reminder_news")
    get_settings.cache_clear()
    uid = signup(client)["user_id"]
    _activate(client, uid, "07700900123")
    code = client.get(f"/api/users/{uid}/referral").json()["code"]
    _invite(client, code, "07700900201")
    sent.clear()
    first = _first_due(db, uid)
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    name, params = next(x for x in sent if x[0] == "bin_reminder_news")
    assert len(params) == 4 and "A neighbour joined" in params[3]
