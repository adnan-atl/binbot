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
    assert res["start_link"] == ("https://wa.me/447700900999?text="
                                 + quote(f"Hi Binny, start my reminders {code}"))
    assert "Hi%20Binny%2C%20start%20my%20reminders%20" in res["start_link"]
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
    assert reply.startswith("Binny here. You're set up: I'll message you the day before each collection, at 7pm.")
    db.expire_all()
    user = db.get(User, uid)
    assert user.status == UserStatus.active and user.start_code is None
    assert _scheduled(db, uid)
    welcome = db.scalar(select(Outbox).where(Outbox.kind == "welcome"))
    assert welcome.to_phone == "+447700900123" and welcome.body == reply
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
    assert _say(client, "+447700900777", "Hi Binny, start my reminders ZZZZZZ") == \
        "Hi! Sign up at localhost:8000 to get bin reminders."


def test_without_a_business_number_reminders_start_straight_away(client, db):
    uid, res = _turn_on(client)
    assert res["status"] == "active" and res["scheduled"] > 0 and "start_link" not in res
    assert db.scalar(select(Outbox).where(Outbox.kind == "welcome"))


def test_page_has_the_say_hi_step(client):
    page = client.get("/").text
    assert 'data-screen="hi"' in page and "Last step:<br>say hi to Binny" in page
    assert 'id="hi-go"' in page and "Say hi on WhatsApp" in page


def test_welcome_ends_with_the_invite_link(client, db, live):
    uid, res = _turn_on(client)
    reply = _say(client, "+447700900123", f"Hi Binny, start my reminders {res['start_code']}")
    ref = client.get(f"/api/users/{uid}/referral").json()
    assert reply.endswith("\n\nYou're a Bin rookie for now. Every neighbour who signs up with your link "
                          "levels you up, and five makes you a certified binfluencer: " + ref["link"])
    assert len(reply) <= 4096
