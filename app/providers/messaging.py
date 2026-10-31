"""
Outbound WhatsApp. One interface, three providers:
  stub   - writes to the outbox table and the log. Default; no credentials needed.
  twilio - Twilio WhatsApp API (sandbox works without Meta approval).
  meta   - WhatsApp Cloud API (needs an approved business account + message templates).
"""
import logging
from typing import Protocol

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Outbox

log = logging.getLogger(__name__)


class SendResult:
    def __init__(self, ok: bool, message_id: str | None = None, error: str | None = None):
        self.ok, self.message_id, self.error = ok, message_id, error


class MessageProvider(Protocol):
    name: str

    def send(self, to_phone: str, body: str) -> SendResult: ...


class StubProvider:
    name = "stub"

    def send(self, to_phone: str, body: str) -> SendResult:
        log.info("[stub whatsapp] -> %s: %s", to_phone, body)
        return SendResult(ok=True, message_id="stub")


class TwilioProvider:
    name = "twilio"

    def send(self, to_phone: str, body: str) -> SendResult:
        s = get_settings()
        r = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json",
            auth=(s.twilio_account_sid, s.twilio_auth_token),
            data={"From": s.twilio_from, "To": f"whatsapp:{to_phone}", "Body": body},
            timeout=15,
        )
        if r.status_code >= 300:
            return SendResult(ok=False, error=r.text[:300])
        return SendResult(ok=True, message_id=r.json().get("sid"))


class MetaProvider:
    name = "meta"

    def send(self, to_phone: str, body: str) -> SendResult:
        s = get_settings()
        # Outside the 24h window Meta requires an approved template; swap `text` for
        # `template` here once templates are approved.
        r = httpx.post(
            f"https://graph.facebook.com/v20.0/{s.meta_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {s.meta_token}"},
            json={"messaging_product": "whatsapp", "to": to_phone.lstrip("+"),
                  "type": "text", "text": {"body": body}},
            timeout=15,
        )
        if r.status_code >= 300:
            return SendResult(ok=False, error=r.text[:300])
        return SendResult(ok=True, message_id=(r.json().get("messages") or [{}])[0].get("id"))


def get_provider() -> MessageProvider:
    name = get_settings().message_provider
    return {"twilio": TwilioProvider, "meta": MetaProvider}.get(name, StubProvider)()


def send_message(db: Session, to_phone: str, body: str, kind: str) -> Outbox:
    """Send and record. Every message lands in `outbox` regardless of provider."""
    p = get_provider()
    res = p.send(to_phone, body)
    row = Outbox(to_phone=to_phone, body=body, kind=kind, provider=p.name,
                 provider_message_id=res.message_id, error=res.error)
    db.add(row)
    db.flush()
    return row
