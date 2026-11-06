"""The team inbox in Slack: what gets posted, and a ✅ on a reply sending it on WhatsApp once."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import InboxReply, InboxThread, Outbox, User
from app.services import inbox

PHONE = "+447700900123"


class FakeSlack:
    def __init__(self):
        self.posts, self.n, self.history = [], 0, {}

    def api(self, method, **body):
        if method == "auth.test":
            return {"ok": True, "user_id": "UBOT", "bot_id": "BBOT"}
        if method == "chat.postMessage":
            self.n += 1
            ts = f"100.{self.n:03d}"
            self.posts.append({**body, "ts": ts})
            self.history[ts] = {"ts": ts, "text": body["text"], "bot_id": "BBOT", "user": "UBOT",
                                **({"thread_ts": body["thread_ts"]} if body.get("thread_ts") else {})}
            return {"ok": True, "channel": "C123", "ts": ts}
        return {"ok": True}

    def get(self, method, **params):
        m = self.history.get(params["ts"])
        return {"ok": True, "messages": [m] if m else []}

    def human_reply(self, thread_ts, text, user="UADNAN", bot_id=None):
        self.n += 1
        ts = f"200.{self.n:03d}"
        self.history[ts] = {"ts": ts, "thread_ts": thread_ts, "text": text, "user": user,
                            **({"bot_id": bot_id} if bot_id else {})}
        return ts


@pytest.fixture
def slack(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    monkeypatch.setenv("SLACK_SIGNUPS_CHANNEL", "")  # on only in the sign-up tracker tests
    get_settings.cache_clear()
    fake = FakeSlack()
    monkeypatch.setattr(inbox, "_api", fake.api)
    monkeypatch.setattr(inbox, "_get", fake.get)
    inbox._me.clear()
    yield fake
    get_settings.cache_clear()


def _on(client):
    uid = client.post("/api/signup/start", json={
        "postcode": "e83pb", "uprn": "10000002", "house_number": "12",
        "address_line": "12, ELM ROAD, LONDON, E8 3PB"}).json()["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123", "consent": True})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


def say(client, text, phone=PHONE):
    return client.post("/api/inbound", json={"phone": phone, "text": text}).json()["reply"]


def react(ts, user="UADNAN", reaction="white_check_mark"):
    inbox.handle_event({"type": "event_callback", "event": {
        "type": "reaction_added", "user": user, "reaction": reaction,
        "item": {"type": "message", "channel": "C123", "ts": ts}}})


def test_unrecognised_message_goes_to_the_inbox_with_context(client, db, slack):
    _on(client)
    say(client, "do you do bulky waste? got an old sofa")
    [card] = slack.posts
    assert card["channel"] == "binjamin-inbox"
    assert card["text"].startswith("*New message* · Elm Road, E8 · ")
    assert "reminders on" in card["text"] and "7pm the evening before" in card["text"]
    assert "…0123" in card["text"] and "+447700900123" not in card["text"]  # never the full number
    assert "Bins: " in card["text"] and "> do you do bulky waste? got an old sofa" in card["text"]
    assert "12" not in card["text"].split("·")[1]  # street only, no house number
    # A second message goes in the same thread
    say(client, "it's a big one")
    assert slack.posts[1]["thread_ts"] == card["ts"]
    assert len(db.scalars(select(InboxThread)).all()) == 1


def test_tick_on_a_reply_sends_it_once(client, db, slack):
    _on(client)
    say(client, "do you do bulky waste?")
    card = slack.posts[0]
    reply_ts = slack.human_reply(card["ts"], "Hi! Hackney collects bulky items: "
                                 "<https://hackney.gov.uk/bulky-waste|book here>. *£40* for 3 items")
    react(reply_ts)
    db.expire_all()
    [sent] = db.scalars(select(Outbox).where(Outbox.kind == "team_reply")).all()
    assert sent.to_phone == PHONE
    assert sent.body == "Hi! Hackney collects bulky items: book here (https://hackney.gov.uk/bulky-waste). *£40* for 3 items"
    assert slack.posts[-1]["text"].startswith("Sent on WhatsApp") and "<@UADNAN>" in slack.posts[-1]["text"]
    react(reply_ts)  # a second ✅
    react(reply_ts, reaction="heavy_check_mark")
    assert len(db.scalars(select(Outbox).where(Outbox.kind == "team_reply")).all()) == 1


def test_claude_draft_can_be_approved_but_our_own_posts_and_other_emojis_cannot(client, db, slack):
    _on(client)
    say(client, "when is christmas collection?")
    card = slack.posts[0]
    react(card["ts"])  # the card (their own words) is never sent back
    follow = slack.posts[-1]["ts"] if len(slack.posts) > 1 else None
    assert follow is None
    draft = slack.human_reply(card["ts"], "<@UCLAUDE> Collections move by a day over Christmas.",
                              user="UCLAUDE", bot_id="BCLAUDE")
    react(draft, reaction="eyes")  # not a tick: nothing
    db.expire_all()
    assert not db.scalars(select(Outbox).where(Outbox.kind == "team_reply")).all()
    react(draft)
    db.expire_all()
    [sent] = db.scalars(select(Outbox).where(Outbox.kind == "team_reply")).all()
    assert sent.body == "Collections move by a day over Christmas."


def test_after_24_hours_it_says_it_cannot_send(client, db, slack):
    uid = _on(client)
    say(client, "hello?")
    card = slack.posts[0]
    u = db.get(User, uid)
    u.last_inbound_at = datetime.now(timezone.utc) - timedelta(hours=25)
    db.commit()
    react(slack.human_reply(card["ts"], "Hi, sorry for the wait"))
    db.expire_all()
    assert not db.scalars(select(Outbox).where(Outbox.kind == "team_reply")).all()
    assert slack.posts[-1]["text"].startswith("Not sent: it's been more than 24 hours")
    assert not db.scalars(select(InboxReply)).all()


def test_their_answer_to_the_team_skips_the_menu(client, db, slack):
    _on(client)
    say(client, "can you help with my council tax?")
    react(slack.human_reply(slack.posts[0]["ts"], "We can't help with that one, sorry"))
    assert say(client, "ok thanks") == ""  # no not-understood menu
    assert slack.posts[-1]["text"] == "*New message*\n> ok thanks"


def test_talk_to_a_human_from_anyone(client, db, slack):
    assert say(client, "can I talk to a human please") == inbox.HANDED_OVER
    assert slack.posts[0]["text"].startswith("*:rotating_light: Wants a person* · not signed up")


def test_wrong_day_reports_are_posted(client, db, slack):
    from app.services.reminders import dispatch_due
    from app.models import Reminder

    uid = _on(client)
    first = db.scalar(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at))
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    say(client, "WRONG")
    assert slack.posts[0]["text"].startswith("*Wrong collection day*")


def test_deleting_the_account_forgets_the_threads(client, db, slack):
    _on(client)
    say(client, "hello there")
    say(client, "DELETE YES")
    db.expire_all()
    assert not db.scalars(select(InboxThread)).all()


def test_off_without_a_token(client, db):
    _on(client)
    say(client, "do you do bulky waste?")
    assert not db.scalars(select(InboxThread)).all()


def _signed(body: bytes, secret="shh", ts=None):
    ts = str(int(ts or time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, "Content-Type": "application/json"}


def test_slack_endpoint_checks_the_signature_and_answers_the_url_check(client, slack):
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    r = client.post("/webhooks/slack", content=body, headers=_signed(body))
    assert r.status_code == 200 and r.text == "abc"
    assert client.post("/webhooks/slack", content=body, headers=_signed(body, "wrong")).status_code == 403
    old = _signed(body, ts=time.time() - 600)
    assert client.post("/webhooks/slack", content=body, headers=old).status_code == 403


def test_every_sign_up_is_posted_to_the_signups_channel(client, db, slack, monkeypatch):
    monkeypatch.setenv("SLACK_SIGNUPS_CHANNEL", "binjamin-signups")
    get_settings.cache_clear()
    _on(client)  # website sign-up, no business number: reminders go on straight away
    [card] = [p for p in slack.posts if p["channel"] == "binjamin-signups"]
    assert card["text"] == ":tada: New sign-up! That's 1 on Binny."
    say(client, "hi", phone="+447700900555")
    for t in ("E8 1AA", "su:addr:1", "su:bins:ok", "su:time:18:00", "su:go:yes"):
        say(client, t, phone="+447700900555")
    assert [p["text"] for p in slack.posts if p["channel"] == "binjamin-signups"][-1] == \
        ":tada: New sign-up! That's 2 on Binny."
