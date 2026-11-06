"""Referrals count when the friend's reminders go on, and the referrer hears about them on their
next reminder (batched), never in a message of its own."""
from datetime import datetime, timedelta, timezone

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
    assert "2 neighbours joined with your link. 8 more and you're officially a Binfluencer." in msg.body
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


def test_news_wording_up_to_and_past_ten(db):
    from app.models import Referral, User
    from app.services.reminders import referral_news

    me = User(referral_code="me")
    db.add(me)
    db.flush()

    def join(k):
        for _ in range(k):
            f = User(referral_code=f"f{len(db.scalars(select(User)).all())}")
            db.add(f)
            db.flush()
            db.add(Referral(referrer_id=me.id, referred_id=f.id))
        db.flush()
        line, rows = referral_news(db, me)
        for r in rows:
            r.announced_at = r.created_at
        return line

    assert join(1) == "\U0001F389 A neighbour joined with your link. 9 more and you're officially a Binfluencer."
    assert join(8).endswith("8 neighbours joined with your link. 1 more and you're officially a Binfluencer.")
    assert join(2).endswith("2 neighbours joined with your link. That's 10: you're officially a Binfluencer!")
    assert join(1).endswith("A neighbour joined with your link. That's 12 neighbours so far.")


def test_whatsapp_invite_link_credits_the_sharer(client, db, monkeypatch):
    from app.core.config import get_settings
    from app.models import Referral
    from app.services.reminders import invite_text

    monkeypatch.setenv("META_DISPLAY_NUMBER", "+44 7700 900999")
    get_settings.cache_clear()
    sharer = signup(client)["user_id"]
    _activate(client, sharer, "07700900123")
    code = client.get(f"/api/users/{sharer}/referral").json()["code"]
    # old binjamin.uk/w/<code> invites now open the website, with the referral saved
    r = client.get(f"/w/{code}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/?ref={code}"
    # an old invite message sent from WhatsApp still credits them
    friend = "+447700900555"
    say = lambda t: client.post("/api/inbound", json={"phone": friend, "text": t}).json()["reply"]
    assert say(invite_text(code)).endswith("What's your postcode?")
    say("E8 1AA")
    say("su:addr:2")
    say("su:bins:ok")
    say("su:time:19:00")
    assert say("su:go:yes").startswith("Binny here. You're all set.")
    db.expire_all()
    assert db.scalar(select(Referral).where(Referral.referrer_id == sharer)) is not None
    monkeypatch.delenv("META_DISPLAY_NUMBER")
    get_settings.cache_clear()


def test_signed_up_person_tapping_an_invite_gets_their_own_link(client, db):
    from app.services.reminders import invite_text

    uid = signup(client)["user_id"]
    _activate(client, uid, "07700900123")
    code = client.get(f"/api/users/{uid}/referral").json()["code"]
    say = lambda t: client.post("/api/inbound", json={"phone": "+447700900123", "text": t}).json()["reply"]
    assert say(invite_text(code)).startswith(
        "You're already signed up, so no need for an invite \U0001F44D Share me with friends")


def test_share_link_counts_people_not_link_previews_and_opens_the_site(client, db):
    from app.models import ChangeLog
    sharer = signup(client)["user_id"]
    code = client.get(f"/api/users/{sharer}/referral").json()["code"]
    assert client.get(f"/api/users/{sharer}/referral").json()["link"].endswith(f"/r/{code}")
    r = client.get(f"/r/{code}", follow_redirects=False,
                   headers={"user-agent": "WhatsApp/2.24.1 A"})  # the preview fetch
    assert r.status_code == 302 and r.headers["location"] == f"/?ref={code}"
    client.get(f"/r/{code}", follow_redirects=False, headers={"user-agent": "Mozilla/5.0 (iPhone)"})
    db.expire_all()
    assert len(db.scalars(select(ChangeLog).where(ChangeLog.kind == "ref_visit",
                                                  ChangeLog.scope_id == sharer)).all()) == 1


def test_home_page_has_a_link_preview(client):
    page = client.get("/").text
    assert 'property="og:image" content="http://localhost:8000/static/og.jpg"' in page
    assert "connects to your council's data" in page
