"""
The team inbox in Slack. Messages Binjamin can't answer (and "talk to a human" requests, and
wrong-day reports) are posted to SLACK_INBOX_CHANNEL with what's needed to reply: street,
status, reminder time, bins. One thread per resident.

Anyone in the channel (or Claude, when tagged) writes a reply in the thread. A ✅ reaction on a
reply sends it to the resident on WhatsApp, as Binjamin, once. Nothing reaches a resident
without a person's ✅. WhatsApp only delivers free text within 24 hours of their last message,
so after that the thread says it couldn't send.

Off (no-op) without SLACK_BOT_TOKEN. Slack is never allowed to break a WhatsApp reply: every
call is wrapped and logged.
"""
import hashlib
import json
import hmac
import html
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import session_factory
from app.models import InboxReply, InboxThread, Reminder, ReminderStatus, User, UserStatus
from app.providers.messaging import send_message

log = logging.getLogger(__name__)

THREAD_DAYS = 7  # a new message after this long starts a new thread
WINDOW = timedelta(hours=24)  # WhatsApp's free-reply window
APPROVE = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}  # ✅ ✔️ ☑️

HUMAN_RE = re.compile(r"\b(human|real person|actual person|speak to (someone|somebody|a person)|"
                      r"talk to (someone|somebody|a person)|(speak|talk|chat) to (an? )?agent|live agent|"
                      r"agent please|customer service)\b", re.I)
HANDED_OVER = "I've passed this on to the team. Someone will reply here soon."


def enabled() -> bool:
    return bool(get_settings().slack_bot_token)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


# ---- Slack Web API ----------------------------------------------------------------------------

def _api(method: str, **body) -> dict:
    """One Slack Web API call. Returns the JSON (ok: false on any failure, never raises)."""
    try:
        r = httpx.post(f"https://slack.com/api/{method}", json=body, timeout=6,
                       headers={"Authorization": f"Bearer {get_settings().slack_bot_token}"})
        data = r.json()
    except Exception as e:  # network, bad JSON
        log.warning("slack %s failed: %s", method, e)
        return {"ok": False, "error": str(e)[:200]}
    if not data.get("ok"):
        log.warning("slack %s: %s", method, data.get("error"))
    return data


def _get(method: str, **params) -> dict:
    try:
        r = httpx.get(f"https://slack.com/api/{method}", params=params, timeout=6,
                      headers={"Authorization": f"Bearer {get_settings().slack_bot_token}"})
        return r.json()
    except Exception as e:
        log.warning("slack %s failed: %s", method, e)
        return {"ok": False, "error": str(e)[:200]}


_me: dict = {}


def _identity() -> dict:
    """Our own bot's user and bot ids (so we never send our own context posts to residents)."""
    if not _me:
        data = _api("auth.test")
        if data.get("ok"):
            _me.update(user_id=data.get("user_id"), bot_id=data.get("bot_id"))
    return _me


def signature_ok(raw: bytes, ts: str | None, sig: str | None, now: float | None = None) -> bool:
    """Slack signs each request: v0=HMAC-SHA256("v0:<timestamp>:<body>", signing secret)."""
    secret = get_settings().slack_signing_secret
    if not secret or not ts or not sig:
        return False
    try:
        if abs((now or time.time()) - int(ts)) > 300:  # replayed or very late
            return False
    except ValueError:
        return False
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + raw,
                                hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ---- residents → Slack ------------------------------------------------------------------------

def note_inbound(db: Session, phone: str) -> None:
    """They messaged us: the 24-hour reply window is open from now."""
    u = db.scalar(select(User).where(User.phone_e164 == phone))
    if u is not None:
        u.last_inbound_at = _now()
        db.commit()


def wants_human(text: str) -> bool:
    return bool(HUMAN_RE.search(text or ""))


def in_conversation(db: Session, phone: str) -> bool:
    """The team replied to them in the last 24 hours: their next message is for the team, so
    Binjamin doesn't answer it with the usual menu."""
    if not enabled():
        return False
    since = _now() - WINDOW
    return bool(db.scalar(select(InboxReply.id).join(InboxThread,
                                                     InboxReply.thread_id == InboxThread.id)
                          .where(InboxThread.phone_e164 == phone, InboxReply.sent_at >= since)))


def post(db: Session, phone: str, text: str, reason: str = "unrecognised") -> None:
    """Put a resident's message in the inbox (reason: unrecognised | human | wrong_day | signup).
    Runs off the webhook's clock; tests and demo mode run it straight away."""
    if not enabled():
        return
    if get_settings().demo_mode:
        _post(phone, text, reason)
    else:
        threading.Thread(target=_post, args=(phone, text, reason), daemon=True).start()


_LABEL = {"unrecognised": "New message", "human": ":rotating_light: Wants a person",
          "wrong_day": "Wrong collection day", "signup": "Message during sign-up"}


def _post(phone: str, text: str, reason: str) -> None:
    db = session_factory()()
    try:
        s = get_settings()
        quoted = "\n".join("> " + line for line in (text.strip() or "(empty)").splitlines())
        since = _now() - timedelta(days=THREAD_DAYS)
        thread = db.scalar(select(InboxThread).where(InboxThread.phone_e164 == phone,
                                                     InboxThread.last_at >= since)
                           .order_by(InboxThread.last_at.desc()))
        if thread is not None:
            data = _api("chat.postMessage", channel=thread.channel, thread_ts=thread.thread_ts,
                        text=f"*{_LABEL.get(reason, 'New message')}*\n{quoted}",
                        reply_broadcast=reason == "human", unfurl_links=False)
            if data.get("ok"):
                thread.last_at = _now()
                db.commit()
            return
        user = db.scalar(select(User).where(User.phone_e164 == phone))
        card = f"*{_LABEL.get(reason, 'New message')}* · {context(db, user, phone)}\n{quoted}"
        data = _api("chat.postMessage", channel=s.slack_inbox_channel, text=card,
                    unfurl_links=False, unfurl_media=False)
        if data.get("ok"):
            db.add(InboxThread(phone_e164=phone, channel=data["channel"], thread_ts=data["ts"]))
            db.commit()
    except Exception:
        log.exception("inbox post failed")
        db.rollback()
    finally:
        db.close()


# ---- sign-ups tracker ------------------------------------------------------------------------

def announce_signup(db: Session, user: User, via: str = "website") -> None:
    """Post a new sign-up alert to SLACK_SIGNUPS_CHANNEL (public or private: the Binjamin app just
    has to be in it) the moment their reminders go on, with the running total. Off without a
    token."""
    s = get_settings()
    if not enabled() or not s.slack_signups_channel:
        return
    try:
        text = signup_card(db, user, via)
    except Exception:
        log.exception("sign-up card failed")
        return
    if s.demo_mode:
        _api("chat.postMessage", channel=s.slack_signups_channel, text=text, unfurl_links=False)
    else:
        threading.Thread(target=_api, args=("chat.postMessage",), daemon=True,
                         kwargs={"channel": s.slack_signups_channel, "text": text,
                                 "unfurl_links": False}).start()


def signup_card(db: Session, user: User, via: str) -> str:
    """The alert, the running total, their council and how long the checker made them wait."""
    from sqlalchemy import func

    total = db.scalar(select(func.count()).select_from(User)
                      .where(User.status == UserStatus.active)) or 0
    lines = [f":tada: New sign-up! That's {total} on {get_settings().brand_name}."]
    council = user.property.council.name if user.property and user.property.council else ""
    secs, source = user.lookup_seconds, user.lookup_source
    wait = ("" if secs is None else
            f"checker failed after {secs:.0f}s, bins added by hand" if source == "manual" else
            f"instant (a neighbour's calendar)" if source == "neighbours" and secs < 2 else
            f"checker took {secs:.0f}s")
    # Bins they added or changed themselves (a different day, a bin the council didn't list)
    from app.models import UserBinSetting
    hand = [b for b in db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == user.id))
            if b.override_rule or b.bin_type_id is None] if getattr(user, "id", None) else []
    edited = (f"{len(hand)} bin{'s' if len(hand) != 1 else ''} added or changed by hand"
              if hand and source != "manual" else "")
    detail = " · ".join(x for x in (council, wait, edited) if x)
    if detail:
        lines.append(detail)
    return "\n".join(lines)


# ---- slow council lookups ---------------------------------------------------------------------

def alert_slow_lookup(prop, secs: float, source: str, error: str | None) -> None:
    """A sign-up's council lookup was slow or failed: tag the team in SLACK_ALERTS_CHANNEL with
    what's needed to look into it (council, how long, the error, the postcode district, what the
    resident got instead). Never the house number or phone number."""
    s = get_settings()
    if not enabled() or not s.slack_alerts_channel:
        return
    council = prop.council.name if prop.council else "unknown council"
    adapter = prop.council.adapter if prop.council else ""
    district = (prop.postcode or "").split(" ")[0]
    outcome = ("failed: they were asked to add their bins by hand" if error
               else "used a neighbour's calendar" if source == "neighbours" else "worked, slowly")
    lines = [f":hourglass: *Slow council lookup* {s.slack_alert_mention}".rstrip(),
             f"{council} · {district} · {secs:.0f}s · {outcome}"]
    if error:
        lines.append(f"Error: `{error[:200]}`")
    if adapter:
        lines.append(f"Scraper: {adapter}")
    text = "\n".join(lines)
    if s.demo_mode:
        _api("chat.postMessage", channel=s.slack_alerts_channel, text=text, unfurl_links=False)
    else:
        threading.Thread(target=_api, args=("chat.postMessage",), daemon=True,
                         kwargs={"channel": s.slack_alerts_channel, "text": text,
                                 "unfurl_links": False}).start()


def _send_error(raw: str | None) -> str:
    """Meta's error JSON as one readable line: '(#132001) Template name does not exist in the
    translation: template name (bin_reminder) does not exist in en_GB'."""
    try:
        e = json.loads(raw or "")["error"]
        msg = e.get("message", "")
        detail = (e.get("error_data") or {}).get("details", "")
        return f"{msg}: {detail}" if detail and detail not in msg else msg or str(raw)
    except Exception:
        return str(raw or "unknown error")


def alert_failed_reminders(db: Session, failures: list) -> None:
    """Reminders that still didn't send after their retries: one message to SLACK_ALERTS_CHANNEL
    tagging the team, so someone can look at it the same evening (a template not approved, an
    expired token, a number that left WhatsApp). failures: (Reminder, error) pairs. Street and
    district plus the number's last four digits, never the full number or house."""
    s = get_settings()
    if not enabled() or not s.slack_alerts_channel or not failures:
        return
    n = len(failures)
    lines = [f":rotating_light: *{n} reminder{'s' if n != 1 else ''} didn't send* "
             f"(gave up after retrying) {s.slack_alert_mention}".rstrip()]
    for r, err in failures[:10]:
        user = db.get(User, r.user_id)
        council = user.property.council.name if user and user.property and user.property.council else ""
        bins = ", ".join(b.get("label", "") for b in r.bins)
        where = " · ".join(x for x in (_street(user), council) if x)
        lines.append(f"• …{(user.phone_e164 or '')[-4:] if user else '????'} · {where} · {bins} "
                     f"for {r.collection_date.strftime('%a %-d %b')}\n   `{_send_error(err)[:200]}`")
    if n > 10:
        lines.append(f"…and {n - 10} more")
    _api("chat.postMessage", channel=s.slack_alerts_channel, text="\n".join(lines),
         unfurl_links=False)


def _street(user: User | None) -> str:
    """Street and postcode district, never the house number: 'Elm Road, E8'."""
    prop = user.property if user else None
    if prop is None:
        return "not signed up yet"
    parts = [p.strip() for p in (prop.address_line or "").split(",") if p.strip()]
    parts = [p for p in parts if not re.search(r"\d", p) or re.search(r"[A-Za-z]{3,}", p)]
    street = next((re.sub(r"^(flat|apartment|unit)?\s*[\w/-]*\d[\w/-]*\s+", "", p, flags=re.I)
                   for p in parts if re.search(r"[A-Za-z]{3,}", p)), "")
    if street.isupper():
        street = street.title()
    district = (prop.postcode or "").split(" ")[0]
    return ", ".join(x for x in (street, district) if x) or district


def context(db: Session, user: User | None, phone: str) -> str:
    """What the team needs to reply, on two or three lines."""
    from app.services.reminders import _clock

    tail = f"…{phone[-4:]}"
    if user is None:
        return f"not signed up · {tail}"
    council = user.property.council.name if user.property and user.property.council else ""
    status = {UserStatus.active: "reminders on", UserStatus.pending: "signing up",
              UserStatus.stopped: "reminders paused"}.get(user.status, user.status.value)
    since = user.created_at.strftime("%-d %b") if user.created_at else ""
    head = " · ".join(x for x in (_street(user), council, f"{status} since {since}" if since else status,
                                  f"{_clock(user.reminder_time)} the evening before"
                                  if user.status == UserStatus.active else "", tail) if x)
    lines = [head]
    if user.property:
        from app.services.signup import bins_out

        bits = []
        for b in bins_out(db, user):
            if not b.enabled:
                continue
            when = "daily, no reminders" if b.daily else (
                f"next {datetime.fromisoformat(b.next).strftime('%a %-d %b')}" if b.next else "no date")
            bits.append(f"{b.label} ({when})")
        if bits:
            lines.append("Bins: " + ", ".join(bits))
    last = db.scalar(select(Reminder).where(Reminder.user_id == user.id,
                                            Reminder.status != ReminderStatus.scheduled,
                                            Reminder.sent_at.is_not(None))
                     .order_by(Reminder.sent_at.desc()))
    if last is not None:
        lines.append(f"Last reminder: {last.collection_date.strftime('%a %-d %b')} collection"
                     + (f", they replied {last.reply}" if last.reply else ""))
    return "\n".join(lines)


# ---- Slack → residents ------------------------------------------------------------------------

def to_whatsapp(text: str) -> str:
    """Slack's message format to WhatsApp's: links, mentions and escapes. *bold*, _italic_ and
    ~strike~ mean the same in both."""
    t = re.sub(r"<@[A-Z0-9]+(\|[^>]*)?>", "", text)  # @mentions (e.g. of Claude)
    t = re.sub(r"<!(here|channel|everyone)[^>]*>", "", t)
    t = re.sub(r"<(https?://[^|>]+)\|([^>]+)>",
               lambda m: m.group(1) if m.group(2) in m.group(1) else f"{m.group(2)} ({m.group(1)})", t)
    t = re.sub(r"<(https?://[^>]+)>", r"\1", t)
    t = re.sub(r"<mailto:([^|>]+)(\|[^>]+)?>", r"\1", t)
    t = t.replace("**", "*")  # markdown bold, if pasted
    return html.unescape(t).strip()


def handle_event(payload: dict) -> None:
    """An event from Slack (already signature-checked). A ✅ on a reply in a resident's thread
    sends it. Runs off the request; tests and demo mode run it straight away."""
    ev = payload.get("event") or {}
    if ev.get("type") != "reaction_added" or ev.get("reaction") not in APPROVE:
        return
    item = ev.get("item") or {}
    if item.get("type") != "message":
        return
    if get_settings().demo_mode:
        _approve(item.get("channel", ""), item.get("ts", ""), ev.get("user", ""))
    else:
        threading.Thread(target=_approve, args=(item.get("channel", ""), item.get("ts", ""),
                                                ev.get("user", "")), daemon=True).start()


def _approve(channel: str, ts: str, approver: str) -> None:
    me = _identity()
    if approver and approver == me.get("user_id"):
        return
    db = session_factory()()
    try:
        if db.scalar(select(InboxReply.id).where(InboxReply.slack_ts == ts)):
            return  # already sent (a second ✅, or Slack retrying)
        found = _get("conversations.replies", channel=channel, ts=ts, limit=1, inclusive="true")
        msgs = found.get("messages") or []
        msg = next((m for m in msgs if m.get("ts") == ts), None)
        if msg is None:
            return
        thread_ts = msg.get("thread_ts")
        if not thread_ts or thread_ts == ts:
            return  # the card itself, or a message outside a thread
        if (me.get("bot_id") and msg.get("bot_id") == me["bot_id"]) or \
                (me.get("user_id") and msg.get("user") == me["user_id"]):
            return  # our own posts (the resident's words) are never sent back to them
        thread = db.scalar(select(InboxThread).where(InboxThread.thread_ts == thread_ts))
        if thread is None:
            return
        text = to_whatsapp(msg.get("text", ""))
        if not text:
            return
        user = db.scalar(select(User).where(User.phone_e164 == thread.phone_e164))
        last_in = _aware(user.last_inbound_at) if user else None
        if user is None:
            _say(channel, thread_ts, "Not sent: this person deleted their account.")
            return
        if last_in is None or _now() - last_in > WINDOW:
            _say(channel, thread_ts, "Not sent: it's been more than 24 hours since they last "
                                     "messaged, so WhatsApp won't deliver a free reply. It'll go "
                                     "through if they message again.")
            return
        row = send_message(db, thread.phone_e164, text[:4096], kind="team_reply")
        if row.error:
            db.rollback()
            _say(channel, thread_ts, f"Couldn't send: {row.error[:200]}")
            return
        db.add(InboxReply(thread_id=thread.id, slack_ts=ts, approved_by=approver or "?"))
        thread.last_at = _now()
        db.commit()
        _say(channel, thread_ts, f"Sent on WhatsApp :white_check_mark: (approved by <@{approver}>)")
    except Exception:
        log.exception("inbox approve failed")
        db.rollback()
    finally:
        db.close()


def _say(channel: str, thread_ts: str, text: str) -> None:
    _api("chat.postMessage", channel=channel, thread_ts=thread_ts, text=text)


def forget(db: Session, phone: str) -> None:
    """Account deletion: drop our records of their inbox threads. (Messages already in Slack
    stay under the workspace's own retention.)"""
    for t in db.scalars(select(InboxThread).where(InboxThread.phone_e164 == phone)):
        for r in db.scalars(select(InboxReply).where(InboxReply.thread_id == t.id)):
            db.delete(r)
        db.delete(t)
    db.flush()
