"""
Outbound WhatsApp. One interface, two providers:
  stub   - writes to the outbox table and the log. Default; no credentials needed.
  meta   - WhatsApp Cloud API. Free text only reaches people inside the 24-hour window after
           their last message; reminders and date changes use approved templates
           (docs/whatsapp-templates.md).
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


class MetaProvider:
    name = "meta"

    def _post(self, payload: dict) -> SendResult:
        s = get_settings()
        r = httpx.post(
            f"https://graph.facebook.com/v20.0/{s.meta_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {s.meta_token}"},
            json={"messaging_product": "whatsapp", **payload},
            timeout=15,
        )
        if r.status_code >= 300:
            return SendResult(ok=False, error=r.text[:300])
        return SendResult(ok=True, message_id=(r.json().get("messages") or [{}])[0].get("id"))

    def send(self, to_phone: str, body: str) -> SendResult:
        """Free text: only delivered inside the 24-hour window after the user's last message."""
        return self._post({"to": to_phone.lstrip("+"), "type": "text", "text": {"body": body}})

    def send_template(self, to_phone: str, name: str, lang: str, body_params: list[str],
                      buttons: bool = False) -> SendResult:
        """An approved template ({{1}}, {{2}}... filled from body_params), deliverable any time.
        With buttons, the template's three quick replies carry DONE / SNOOZE / WRONG payloads."""
        components = [{"type": "body",
                       "parameters": [{"type": "text", "text": p} for p in body_params]}]
        if buttons:
            components += [{"type": "button", "sub_type": "quick_reply", "index": str(i),
                            "parameters": [{"type": "payload", "payload": payload}]}
                           for i, payload in enumerate(QUICK_REPLIES)]
        return self._post({"to": to_phone.lstrip("+"), "type": "template",
                           "template": {"name": name, "language": {"code": lang},
                                        "components": components}})


# Payloads on the reminder template's quick-reply buttons ("Done", "Snooze", "Wrong day"),
# in button order. Replies come back through the webhook as these words.
QUICK_REPLIES = ("DONE", "SNOOZE", "WRONG")


def get_provider() -> MessageProvider:
    name = get_settings().message_provider
    return {"meta": MetaProvider}.get(name, StubProvider)()


def send_message(db: Session, to_phone: str, body: str, kind: str) -> Outbox:
    """Send and record. Every message lands in `outbox` regardless of provider."""
    p = get_provider()
    res = p.send(to_phone, body)
    row = Outbox(to_phone=to_phone, body=body, kind=kind, provider=p.name,
                 provider_message_id=res.message_id, error=res.error)
    db.add(row)
    db.flush()
    return row


def uses_template(template: str) -> bool:
    """True when a template is configured and the active provider can send templates."""
    return bool(template) and hasattr(get_provider(), "send_template")


def send_template_message(db: Session, to_phone: str, template: str, params: list[str],
                          body: str, kind: str, buttons: bool = False) -> Outbox:
    """Send an approved template when one is configured and the provider supports templates;
    otherwise send `body` as free text. `body` is what's recorded in the outbox either way."""
    p = get_provider()
    if uses_template(template):
        res = p.send_template(to_phone, template, get_settings().meta_template_lang, params, buttons)
    else:
        res = p.send(to_phone, body)
    row = Outbox(to_phone=to_phone, body=body, kind=kind, provider=p.name,
                 provider_message_id=res.message_id, error=res.error)
    db.add(row)
    db.flush()
    return row
