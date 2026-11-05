"""Reminders and date changes go out as approved WhatsApp templates (they're sent outside the
24-hour reply window); quick-reply taps come back through the webhook as DONE / SNOOZE / menu:settings."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import BinType, Outbox, Reminder, ReminderStatus, User
from app.providers import messaging
from app.services.reminders import dispatch_due
from app.workers import monitor
from tests.test_flow import signup

META_ENV = {"MESSAGE_PROVIDER": "meta", "META_TOKEN": "tok", "META_PHONE_NUMBER_ID": "123"}


class FakeResponse:
    status_code = 200

    def json(self):
        return {"messages": [{"id": "wamid.1"}]}


@pytest.fixture
def sent(monkeypatch):
    """Meta provider with httpx.post captured instead of sent."""
    calls = []
    for k, v in META_ENV.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr(messaging.httpx, "post",
                        lambda url, headers, json, timeout: calls.append({"url": url, "json": json}) or FakeResponse())
    yield calls
    for k in [*META_ENV, "META_REMINDER_TEMPLATE", "META_CHANGE_TEMPLATE"]:
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()


def _signed_up(client):
    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123"})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


def test_reminder_goes_out_as_a_template_with_quick_replies(client, db, sent):
    _signed_up(client)
    first = db.scalar(select(Reminder).order_by(Reminder.send_at))
    assert dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1)) == 1
    msg = sent[-1]
    assert msg["url"] == "https://graph.facebook.com/v20.0/123/messages"
    from app.services.normalize import bin_tag
    names = [bin_tag(b) for b in first.bins]  # each bin with its colour dot
    bins = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    assert msg["json"] == {
        "messaging_product": "whatsapp", "to": "447700900123", "type": "template",
        "template": {
            "name": "bin_reminder", "language": {"code": "en_GB"},
            "components": [
                {"type": "body", "parameters": [
                    {"type": "text", "text": bins},
                    {"type": "text", "text": "tomorrow"},
                    {"type": "text", "text": first.collection_date.strftime("%a %-d %b")}]},
                {"type": "button", "sub_type": "quick_reply", "index": "0",
                 "parameters": [{"type": "payload", "payload": "DONE"}]},
                {"type": "button", "sub_type": "quick_reply", "index": "1",
                 "parameters": [{"type": "payload", "payload": "SNOOZE"}]},
                {"type": "button", "sub_type": "quick_reply", "index": "2",
                 "parameters": [{"type": "payload", "payload": "menu:settings"}]},
            ]}}
    # The outbox still records the readable text
    row = db.scalar(select(Outbox).where(Outbox.kind == "reminder"))
    assert row.provider == "meta" and row.provider_message_id == "wamid.1" and "go" in row.body


def test_empty_template_name_sends_free_text(client, db, sent, monkeypatch):
    monkeypatch.setenv("META_REMINDER_TEMPLATE", "")
    get_settings.cache_clear()
    _signed_up(client)
    first = db.scalar(select(Reminder).order_by(Reminder.send_at))
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    assert sent[-1]["json"]["type"] == "text"
    assert "Reply DONE when it's out" in sent[-1]["json"]["text"]["body"]


def test_date_change_goes_out_as_a_template(client, db, sent):
    uid = _signed_up(client)
    prop = db.get(User, uid).property
    bt = db.scalar(select(BinType))
    frm, to = date.today() + timedelta(days=8), date.today() + timedelta(days=9)
    n = monitor._propagate_change(db, prop, {"moved": [{"bin_type_id": bt.id, "from": frm.isoformat(),
                                                        "to": to.isoformat()}]})
    assert n == 1
    t = sent[-1]["json"]["template"]
    assert t["name"] == "bin_day_changed" and t["language"] == {"code": "en_GB"}
    assert t["components"] == [{"type": "body", "parameters": [
        {"type": "text", "text": bt.council_label},
        {"type": "text", "text": to.strftime("%a %-d %b")},
        {"type": "text", "text": frm.strftime("%a %-d %b")}]}]


def _hook(client, message):
    return client.post("/webhooks/meta", json={"entry": [{"changes": [{"value": {"messages": [
        {"from": "447700900123", **message}]}}]}]})


@pytest.mark.parametrize("message,status", [
    ({"type": "button", "button": {"text": "Done", "payload": "DONE"}}, ReminderStatus.done),
    ({"type": "button", "button": {"text": "Snooze"}}, ReminderStatus.snoozed),
    ({"type": "interactive", "interactive": {"type": "button_reply",
                                             "button_reply": {"id": "DONE", "title": "Done"}}},
     ReminderStatus.done),
    ({"type": "interactive", "interactive": {"type": "button_reply",
                                             "button_reply": {"title": "Snooze"}}},
     ReminderStatus.snoozed),
    ({"type": "text", "text": {"body": "done"}}, ReminderStatus.done),
])
def test_button_replies_work_like_typed_words(client, db, message, status):
    _signed_up(client)
    first = db.scalar(select(Reminder).order_by(Reminder.send_at))
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    assert _hook(client, message).json() == {"ok": True}
    db.expire_all()
    assert db.get(Reminder, first.id).status == status


def test_wrong_day_button_is_a_wrong_vote(client, db):
    _signed_up(client)
    first = db.scalar(select(Reminder).order_by(Reminder.send_at))
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    _hook(client, {"type": "button", "button": {"text": "Wrong day"}})
    db.expire_all()
    assert db.get(Reminder, first.id).reply == "wrong"
    reply = db.scalars(select(Outbox).where(Outbox.kind == "reply")).all()[-1]
    assert reply.body.startswith("Thanks, noted.")
