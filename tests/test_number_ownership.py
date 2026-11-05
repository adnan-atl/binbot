"""With a business number set, sign-up ends with the resident saying hi from their own WhatsApp:
nothing is sent to a typed number until then, and the number that says hi is the one we keep."""
from urllib.parse import quote

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Outbox, Reminder, ReminderStatus, User, UserStatus
from tests.test_flow import signup


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("META_DISPLAY_NUMBER", "+44 7700 900999")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("META_DISPLAY_NUMBER")
    get_settings.cache_clear()


def _turn_on(client, phone="07700900123"):
    uid = signup(client)["user_id"]
    assert client.post(f"/api/users/{uid}/phone", json={"phone": phone}).status_code == 200
    r = client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    assert r.status_code == 200
    return uid, r.json()


def _say(client, phone, text):
    return client.post("/api/inbound", json={"phone": phone, "text": text}).json()["reply"]


def _scheduled(db, uid):
    db.expire_all()
    return db.scalars(select(Reminder).where(Reminder.user_id == uid,
                                             Reminder.status == ReminderStatus.scheduled)).all()


def test_turning_on_waits_for_hi_and_sends_nothing(client, db, live):
    uid, res = _turn_on(client)
    code = res["start_code"]
    assert res["status"] == "awaiting_hi" and res["scheduled"] == 0
    assert len(code) == 6 and code.isalnum() and code.isupper()
    # No code in the message: we know them by the number they message from
    assert res["start_link"] == "https://wa.me/447700900999?text=" + quote("Hey Binny, I'M BIN")
    assert code not in res["start_link"]
    user = db.get(User, uid)
    assert user.status == UserStatus.pending and user.start_code == code
    assert _scheduled(db, uid) == []
    assert db.scalar(select(Outbox)) is None  # nothing sent to the typed number
    # Asking again keeps the same code
    again = client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"}).json()
    assert again["start_code"] == code


def test_hi_from_the_typed_number_starts_reminders_and_sends_the_welcome(client, db, live):
    uid, res = _turn_on(client)
    reply = _say(client, "+447700900123", f"Hi Binny, start my reminders {res['start_code']}")
    assert reply.startswith("Binny here. You're all set. I'll message you at 7pm the evening before collection.")
    db.expire_all()
    user = db.get(User, uid)
    assert user.status == UserStatus.active and user.start_code is None
    assert _scheduled(db, uid)
    welcome = db.scalar(select(Outbox).where(Outbox.kind == "welcome"))
    assert welcome.to_phone == "+447700900123" and reply.startswith(welcome.body)
    # Sent as separate messages: set up, first collection, how to stop, the invite
    more = [o.body for o in db.scalars(select(Outbox).where(Outbox.kind == "welcome_more"))]
    assert len(more) == 2 and reply == "\n\n".join(
        [welcome.body] + sorted(more, key=lambda b: not b.startswith("First up")))
    assert "Want to bin me off? Reply STOP anytime." in more
    # A second tap just says it's already on
    assert _say(client, "+447700900123", f"Hi Binny, start my reminders {res['start_code']}") == \
        'Your reminders are already on. Message "settings" to change them.'


def test_code_is_case_insensitive_and_wording_tolerant(client, db, live):
    uid, res = _turn_on(client)
    _say(client, "+447700900123", f"hello! START MY REMINDERS {res['start_code'].lower()} thanks")
    db.expire_all()
    assert db.get(User, uid).status == UserStatus.active


def test_hi_from_a_different_number_moves_the_user_to_that_number(client, db, live):
    uid, res = _turn_on(client, phone="07700900123")
    _say(client, "+447700900456", f"Hi Binny, start my reminders {res['start_code']}")
    db.expire_all()
    user = db.get(User, uid)
    assert user.phone_e164 == "+447700900456" and user.status == UserStatus.active
    assert db.scalar(select(Outbox).where(Outbox.kind == "welcome")).to_phone == "+447700900456"


def test_hi_from_a_number_another_account_holds_takes_it_over(client, db, live):
    old_uid, old = _turn_on(client, phone="07700900456")
    _say(client, "+447700900456", f"Hi Binny, start my reminders {old['start_code']}")
    new_uid, new = _turn_on(client, phone="07700900123")
    _say(client, "+447700900456", f"Hi Binny, start my reminders {new['start_code']}")
    db.expire_all()
    old_user, new_user = db.get(User, old_uid), db.get(User, new_uid)
    assert new_user.phone_e164 == "+447700900456" and new_user.status == UserStatus.active
    assert old_user.phone_e164 is None and old_user.status == UserStatus.stopped
    assert _scheduled(db, old_uid) == []


def test_a_typed_number_no_one_proved_can_be_typed_again(client, db, live):
    first_uid, _ = _turn_on(client, phone="07700900123")
    second_uid, _ = _turn_on(client, phone="07700900123")  # no 409: the first never said hi
    db.expire_all()
    assert db.get(User, first_uid).phone_e164 is None
    assert db.get(User, second_uid).phone_e164 == "+447700900123"


def test_unknown_code_from_unknown_number_gets_the_sign_up_reply(client, live):
    # A code we don't know: they start signing up in WhatsApp instead
    assert _say(client, "+447700900777", "Hi Binny, start my reminders ZZZZZZ").endswith(
        "What's your postcode?")


def test_without_a_business_number_reminders_start_straight_away(client, db):
    uid, res = _turn_on(client)
    assert res["status"] == "active" and res["scheduled"] > 0 and "start_link" not in res
    assert db.scalar(select(Outbox).where(Outbox.kind == "welcome"))


def test_page_has_the_say_hi_step(client):
    page = client.get("/").text
    assert 'data-screen="hi"' in page and "Last step:<br>say hi to Binny" in page
    assert 'id="hi-go"' in page and "Say hi on WhatsApp" in page


def test_welcome_has_no_invite_link(client, db, live):
    uid, res = _turn_on(client)
    reply = _say(client, "+447700900123", f"Hi Binny, start my reminders {res['start_code']}")
    assert reply.endswith("\n\nWant to bin me off? Reply STOP anytime.")
    assert "/?ref=" not in reply and "rookie" not in reply


def test_computer_sign_up_gets_a_qr_and_a_status_to_wait_on(client, db, live):
    uid, _ = _turn_on(client)
    assert client.get(f"/api/users/{uid}/status").json()["status"] == "pending"
    r = client.get(f"/api/users/{uid}/start-qr.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in r.content
    code = db.scalar(select(User).where(User.id == uid)).start_code
    _say(client, "+447700900123", f"Hi Binny, start my reminders {code}")
    assert client.get(f"/api/users/{uid}/status").json()["status"] == "active"
    assert client.get(f"/api/users/{uid}/start-qr.svg").status_code == 404  # code used up


def test_im_bin_from_the_typed_number_starts_reminders(client, db, live):
    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123", "consent": True})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    reply = _say(client, "+447700900123", "Hey Binny, I'M BIN")
    assert reply.startswith("Binny here. You're all set.")
    db.expire_all()
    assert db.get(User, uid).status == UserStatus.active
    assert _say(client, "+447700900123", "Hey Binny, I'M BIN").startswith("Your reminders are already on")


def test_im_bin_from_another_number_claims_the_sign_up_by_postcode(client, db, live):
    uid = signup(client)["user_id"]  # postcode E8 3PB
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123", "consent": True})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    ask = _say(client, "+447700900888", "hey binjamin i'm bin")
    assert ask.startswith("Hi! I couldn't find a sign-up for this number.")
    assert _say(client, "+447700900888", "e8 3pb").startswith("Binny here. You're all set.")
    db.expire_all()
    u = db.get(User, uid)
    assert u.phone_e164 == "+447700900888" and u.status == UserStatus.active
    assert len(db.scalars(select(User)).all()) == 1  # the placeholder for the new number is gone


def test_claim_with_no_matching_sign_up_carries_on_as_a_whatsapp_sign_up(client, db, live):
    _say(client, "+447700900888", "Hey Binny, I'M BIN")
    assert _say(client, "+447700900888", "E8 1AA").startswith("Which one is your home?")
