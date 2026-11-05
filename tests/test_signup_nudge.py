"""Website sign-ups that reached the say-hi step but never sent the message get one nudge with
a Start reminders button; tapping it is their hi."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.models import Outbox, User, UserStatus
from app.providers import messaging
from app.services.reminders import START_PAYLOAD, signup_nudges
from tests.test_flow import signup
from tests.test_number_ownership import _say, live  # noqa: F401

PHONE = "+447700900123"


def _stuck(client, consent=True):
    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123", "consent": consent})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


def _nudges(db):
    db.expire_all()
    return db.scalars(select(Outbox).where(Outbox.kind == "signup_nudge")).all()


def _at(db, uid, minutes):
    return db.get(User, uid).hi_prompted_at.replace(tzinfo=timezone.utc) + timedelta(minutes=minutes)


def test_one_nudge_after_12_minutes_then_the_button_starts_reminders(client, db, live):  # noqa: F811
    uid = _stuck(client)
    assert signup_nudges(db, now_utc=_at(db, uid, 5)) == 0
    assert signup_nudges(db, now_utc=_at(db, uid, 13)) == 1
    [n] = _nudges(db)
    assert n.to_phone == PHONE and n.body.startswith("Your bin reminders are nearly ready.")
    assert signup_nudges(db, now_utc=_at(db, uid, 30)) == 0  # only once
    assert _say(client, PHONE, START_PAYLOAD).startswith("Binny here. You're all set.")
    db.expire_all()
    assert db.get(User, uid).status == UserStatus.active


def test_no_nudge_once_they_said_hi_or_without_consent(client, db, live):  # noqa: F811
    uid = _stuck(client)
    _say(client, PHONE, "Hey Binny, I'M BIN")
    assert signup_nudges(db, now_utc=_at(db, uid, 20)) == 0
    db.query(User).delete()
    db.commit()
    uid = _stuck(client, consent=False)
    assert signup_nudges(db, now_utc=_at(db, uid, 20)) == 0


def test_real_whatsapp_sends_the_template_with_its_button(client, db, live, monkeypatch):  # noqa: F811
    class FakeMeta(messaging.StubProvider):
        name = "meta"
        sent = None

        def send_template(self, to_phone, name, lang, body_params, buttons=False):
            FakeMeta.sent = (name, body_params, buttons)
            return messaging.SendResult(ok=True, message_id="t")

    uid = _stuck(client)
    fake = FakeMeta()
    monkeypatch.setattr(messaging, "get_provider", lambda: fake)
    monkeypatch.setattr("app.services.reminders.get_provider", lambda: fake)
    assert signup_nudges(db, now_utc=_at(db, uid, 13)) == 0  # no approved template yet
    monkeypatch.setenv("META_SIGNUP_NUDGE_TEMPLATE", "bin_signup_nudge")
    get_settings.cache_clear()
    assert signup_nudges(db, now_utc=_at(db, uid, 13)) == 1
    assert FakeMeta.sent == ("bin_signup_nudge", [], [START_PAYLOAD])
