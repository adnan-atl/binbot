"""Signing up inside WhatsApp: hi → postcode → address → bins → time → terms → reminders on.
The address-to-calendar steps are the website's own (services/signup.py)."""
from sqlalchemy import select

from app.core.guard import reset_limits
from app.models import ChangeLog, Outbox, Reminder, User, UserStatus
from tests.test_number_ownership import _say, _scheduled, _turn_on, live  # noqa: F401
from tests.test_flow import signup

PHONE = "+447700900555"


def say(client, text, phone=PHONE):
    return _say(client, phone, text)


def last_sent(db, phone=PHONE):
    db.expire_all()
    return db.scalars(select(Outbox).where(Outbox.to_phone == phone)
                      .order_by(Outbox.created_at.desc(), Outbox.id.desc())).first().body


def sent(db, phone=PHONE):
    db.expire_all()
    return [o.body for o in db.scalars(select(Outbox).where(Outbox.to_phone == phone)
                                       .order_by(Outbox.created_at))]


def me(db, phone=PHONE):
    db.expire_all()
    return db.scalar(select(User).where(User.phone_e164 == phone))


def to_bins(client, db):
    assert say(client, "hello").endswith("What's your postcode?")
    menu = say(client, "e8 1aa")
    assert menu.startswith("Which one is your home?")
    assert "10, Elm Road" in last_sent(db) and "Not listed" in last_sent(db)
    assert say(client, "su:addr:1") == ""  # the answer comes as its own messages
    return sent(db)


def test_full_sign_up_in_whatsapp(client, db):
    msgs = to_bins(client, db)
    assert any(m.startswith("On it. Reading Hackney's bin calendar") for m in msgs)
    bins = msgs[-1]
    assert bins.startswith("Here's your bin calendar for 12, Elm Road:")
    assert "*Blue recycling bin*: Every 2 weeks on Tuesday. Next: Tue" in bins and "[Looks right] [Something's off]" in bins
    u = me(db)
    assert u.status == UserStatus.pending and u.property.uprn == "10000002"
    assert say(client, "su:bins:ok").startswith("When should I nudge you?")
    assert "4pm" in last_sent(db) and "9pm" in last_sent(db)
    consent = say(client, "su:time:18:00")
    assert consent.startswith("Last step.") and "terms (" in consent and "privacy policy (" in consent
    assert "[Start reminders] [No feature updates]" in last_sent(db)
    welcome = say(client, "su:go:yes")
    assert welcome.startswith("Binny here. You're all set. I'll message you at 6pm the day before collection.")
    u = me(db)
    assert u.status == UserStatus.active and u.reminder_time == "18:00"
    assert u.consent_at and u.marketing_consent_at
    assert u.wa_step is None and u.wa_data is None
    assert db.scalars(select(Reminder).where(Reminder.user_id == u.id)).all()
    db.expire_all()
    assert db.scalar(select(Outbox).where(Outbox.kind == "welcome", Outbox.to_phone == PHONE))
    # Signed up: from now on it's the usual commands
    assert say(client, "settings").startswith("Your reminders: the day before at 6pm")


def test_no_feature_updates_still_starts_reminders_without_marketing(client, db):
    to_bins(client, db)
    say(client, "su:bins:ok")
    say(client, "7pm")  # typed instead of tapped
    assert say(client, "su:go:nomkt").startswith("Binny here. You're all set.")
    u = me(db)
    assert u.status == UserStatus.active and u.reminder_time == "19:00"
    assert u.consent_at and u.marketing_consent_at is None


def test_leading_with_the_postcode_skips_the_hello(client, db):
    assert say(client, "E8 1AA").startswith("Which one is your home?")


def test_typed_house_number_and_not_listed(client, db):
    say(client, "hi")
    say(client, "E8 1AA")
    assert say(client, "14") == ""  # one match: straight to the check
    assert me(db).property.uprn == "10000003"
    say(client, "restart")
    say(client, "E8 1AA")
    assert say(client, "su:addr:none") == "What's your house number or name?"
    assert say(client, "Rose Cottage") == ""
    u = me(db)
    assert u.property.uprn is None and u.property.house_number == "Rose Cottage"


def test_bad_postcode_and_chat(client, db):
    say(client, "hi")
    assert say(client, "E8 1A") == "That doesn't look like a UK postcode. Try again, like E8 1AA."
    assert say(client, "what is this?") == "Let's get you set up. What's your postcode?"


def test_something_is_off_offers_another_address_or_carry_on(client, db):
    to_bins(client, db)
    off = say(client, "su:bins:off")
    assert off.startswith("No problem.") and "[Change address] [Carry on]" in last_sent(db)
    db.expire_all()
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "signup_off"))
    assert say(client, "su:bins:redo").endswith("What's your postcode?")
    assert me(db).wa_step == "postcode"


def test_stop_mid_sign_up_forgets_them(client, db):
    say(client, "hi")
    say(client, "E8 1AA")
    assert say(client, "STOP") == "OK, I've stopped. Message me any time to start again."
    assert me(db) is None
    assert not sent(db)  # the messages we sent them go too


def test_council_checks_are_rate_limited(client, db, monkeypatch):
    monkeypatch.setenv("RATE_LIMITS", "true")
    from app.core.config import get_settings
    get_settings.cache_clear()
    reset_limits()
    say(client, "E8 1AA")
    for _ in range(4):
        say(client, "su:addr:0")
        say(client, "su:bins:redo")
        say(client, "E8 1AA")
    assert say(client, "su:addr:0").startswith("That's a lot of lookups")
    reset_limits()


def test_website_sign_up_that_messages_without_the_code(client, db, live):  # noqa: F811
    uid = signup(client)["user_id"]
    assert client.post(f"/api/users/{uid}/phone",
                       json={"phone": "07700900123", "consent": True}).status_code == 200
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "20:00"})
    ask = _say(client, "+447700900123", "hello?")
    assert ask.startswith("Looks like you started signing up on the website.") and "8pm" in ask
    assert _say(client, "+447700900123", "su:web:yes").startswith("Binny here. You're all set.")
    db.expire_all()
    u = db.get(User, uid)
    assert u.status == UserStatus.active and u.start_code is None
    assert _scheduled(db, uid)


def test_website_and_whatsapp_share_the_same_bins(client, db):
    """Same address, same calendar, same bins: both go through services/signup.py."""
    web = signup(client)
    to_bins(client, db)
    wa = me(db)
    web_bins = client.get(f"/api/users/{web['user_id']}/bins").json()
    wa_bins = client.get(f"/api/users/{wa.id}/bins").json()
    assert [(b["label"], b["days"], b["next"]) for b in web_bins] == \
        [(b["label"], b["days"], b["next"]) for b in wa_bins]
