"""
Outbound WhatsApp. One interface, two providers:
  stub   - writes to the outbox table and the log. Default; no credentials needed.
  meta   - WhatsApp Cloud API. Free text only reaches people inside the 24-hour window after
           their last message; reminders and date changes use approved templates
           (docs/whatsapp-templates.md).
"""
import logging
from concurrent.futures import ThreadPoolExecutor
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

    def send_buttons(self, to_phone: str, body: str, buttons: list[tuple[str, str]]) -> SendResult:
        return self.send(to_phone, readable(body, buttons=buttons))

    def send_list(self, to_phone: str, body: str, button_label: str,
                  sections: list[tuple[str, list[tuple[str, str, str]]]]) -> SendResult:
        return self.send(to_phone, readable(body, sections=sections))

    def send_link_button(self, to_phone: str, body: str, label: str, url: str) -> SendResult:
        return self.send(to_phone, readable(body, link=(label, url)))

    def send_contact(self, to_phone: str, card: dict) -> SendResult:
        return self.send(to_phone, contact_readable(card))


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
        # A link in the text gets WhatsApp's preview card (Binjamin's picture for binjamin.uk links)
        text = {"body": body, **({"preview_url": True} if "https://" in body else {})}
        return self._post({"to": to_phone.lstrip("+"), "type": "text", "text": text})

    def send_template(self, to_phone: str, name: str, lang: str, body_params: list[str],
                      buttons=False) -> SendResult:
        """An approved template ({{1}}, {{2}}... filled from body_params), deliverable any time.
        With buttons=True, the template's quick replies carry the QUICK_REPLIES payloads, in order;
        a list of payloads instead fills that template's own quick replies."""
        components = [{"type": "body",
                       "parameters": [{"type": "text", "text": p} for p in body_params]}]
        if buttons:
            components += [{"type": "button", "sub_type": "quick_reply", "index": str(i),
                            "parameters": [{"type": "payload", "payload": payload}]}
                           for i, payload in enumerate(
                               QUICK_REPLIES if buttons is True else buttons)]
        return self._post({"to": to_phone.lstrip("+"), "type": "template",
                           "template": {"name": name, "language": {"code": lang},
                                        "components": components}})

    def send_buttons(self, to_phone: str, body: str, buttons: list[tuple[str, str]]) -> SendResult:
        """Up to 3 reply buttons (id, title <= 20 chars). Free-form: only inside the 24-hour
        window, which is open whenever the resident has just messaged or tapped."""
        return self._post({"to": to_phone.lstrip("+"), "type": "interactive", "interactive": {
            "type": "button", "body": {"text": body},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}}
                                   for i, t in buttons[:3]]}}})

    def send_contact(self, to_phone: str, card: dict) -> SendResult:
        """A contact card (WhatsApp shows Binjamin's business card, with Message). People can
        forward it to anyone without leaving WhatsApp. Free-form: inside the 24-hour window."""
        return self._post({"to": to_phone.lstrip("+"), "type": "contacts", "contacts": [card]})

    def send_link_button(self, to_phone: str, body: str, label: str, url: str) -> SendResult:
        """One button that opens a web link (label <= 20 chars). Free-form: inside the 24-hour
        window only."""
        return self._post({"to": to_phone.lstrip("+"), "type": "interactive", "interactive": {
            "type": "cta_url", "body": {"text": body},
            "action": {"name": "cta_url",
                       "parameters": {"display_text": label[:20], "url": url}}}})

    def send_list(self, to_phone: str, body: str, button_label: str,
                  sections: list[tuple[str, list[tuple[str, str, str]]]]) -> SendResult:
        """A list menu: up to 10 rows in all (id, title <= 24, description <= 72)."""
        out, left = [], 10
        for title, rows in sections:
            rows = rows[:left]
            left -= len(rows)
            if rows:
                out.append({"title": title[:24], "rows": [
                    {"id": i, "title": t[:24], **({"description": d[:72]} if d else {})}
                    for i, t, d in rows]})
        return self._post({"to": to_phone.lstrip("+"), "type": "interactive", "interactive": {
            "type": "list", "body": {"text": body},
            "action": {"button": button_label[:20], "sections": out}}})


# Payloads on the reminder template's quick-reply buttons ("Done", "Snooze", "Settings"), in
# button order. Taps come back through the webhook as these words. "Wrong collection day" lives
# in the Settings menu.
QUICK_REPLIES = ("DONE", "SNOOZE", "menu:settings")


def contact_readable(card: dict) -> str:
    phone = (card.get("phones") or [{}])[0].get("phone", "")
    return f"[Contact card: {card.get('name', {}).get('formatted_name', '')} {phone}]"


def send_contact(db: Session, to_phone: str, card: dict, kind: str) -> Outbox:
    """Send a contact card and record it."""
    p = get_provider()
    res = p.send_contact(to_phone, card)
    row = Outbox(to_phone=to_phone, body=contact_readable(card), kind=kind, provider=p.name,
                 provider_message_id=res.message_id, error=res.error)
    db.add(row)
    db.flush()
    return row


def readable(body: str, buttons=(), sections=(), link=None) -> str:
    """What a menu message looks like as plain text: for the outbox, logs and tests."""
    lines = [body]
    if link:
        lines.append(f"[{link[0]} -> {link[1]}]")
    if buttons:
        lines.append(" ".join(f"[{t}]" for _, t in buttons))
    for title, rows in sections:
        lines.append(f"{title}:")
        lines += [f"- {t}" + (f" ({d})" if d else "") for _, t, d in rows]
    return "\n".join(lines)


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


def _deliver_template(p, to_phone: str, template: str, params: list[str], body: str,
                      buttons) -> SendResult:
    """The network part only (no database), so it can run on a worker thread."""
    try:
        if uses_template(template):
            return p.send_template(to_phone, template, get_settings().meta_template_lang, params, buttons)
        return p.send(to_phone, body)
    except Exception as e:  # a timeout or dropped connection fails this message, not the batch
        log.warning("send to %s failed: %s", to_phone[-4:], e)
        return SendResult(ok=False, error=str(e)[:300])


def _record(db: Session, p, to_phone: str, body: str, kind: str, res: SendResult) -> Outbox:
    row = Outbox(to_phone=to_phone, body=body, kind=kind, provider=p.name,
                 provider_message_id=res.message_id, error=res.error)
    db.add(row)
    db.flush()
    return row


def send_template_message(db: Session, to_phone: str, template: str, params: list[str],
                          body: str, kind: str, buttons=False) -> Outbox:
    """Send an approved template when one is configured and the provider supports templates;
    otherwise send `body` as free text. `body` is what's recorded in the outbox either way."""
    p = get_provider()
    return _record(db, p, to_phone, body, kind, _deliver_template(p, to_phone, template, params,
                                                                   body, buttons))


# Messages sent at once by send_template_batch. Each send is a ~0.2s HTTP call, so 10 at once is
# about 50 a second (3,000 a minute), under Meta's default limit of 80 a second per number.
SEND_CONCURRENCY = 10


def send_template_batch(db: Session, template: str, jobs: list[tuple[str, list[str], str]],
                        kind: str, buttons=False) -> list[Outbox]:
    """send_template_message for many people at once: jobs are (phone, params, body). The HTTP
    calls run SEND_CONCURRENCY at a time; the outbox rows are written here, on the caller's
    thread, in the same order as jobs."""
    p = get_provider()
    with ThreadPoolExecutor(max_workers=SEND_CONCURRENCY) as pool:
        results = list(pool.map(lambda j: _deliver_template(p, j[0], template, j[1], j[2], buttons),
                                jobs))
    return [_record(db, p, phone, body, kind, res) for (phone, _, body), res in zip(jobs, results)]


def send_reply(db: Session, to_phone: str, text: str, kind: str, buttons=(), list_button: str = "",
               sections=(), link=None) -> Outbox:
    """Send a reply that may carry tap options: a list menu, reply buttons, or plain text.
    The outbox records the readable version (body plus options)."""
    p = get_provider()
    if link:  # (label, url): one button that opens a web link
        res = p.send_link_button(to_phone, text, link[0], link[1])
    elif sections:
        res = p.send_list(to_phone, text, list_button, list(sections))
    elif buttons:
        res = p.send_buttons(to_phone, text, list(buttons))
    else:
        res = p.send(to_phone, text)
    row = Outbox(to_phone=to_phone, body=readable(text, buttons, sections, link), kind=kind,
                 provider=p.name, provider_message_id=res.message_id, error=res.error)
    db.add(row)
    db.flush()
    return row
