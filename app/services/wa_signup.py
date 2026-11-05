"""
Signing up inside WhatsApp, for people who message us before (or instead of) using the website:

  hi → postcode → pick your address → bins ("Looks right?") → reminder time → terms → on

The address, calendar, bins and switching-on steps are the website's own (services/signup.py),
so a change there applies here too. Only the conversation lives in this file. Where they are in
it is kept on a pending User (wa_step, wa_data), created when they first message us: the number
is theirs, since they're messaging from it.

Also here: someone who started on the website, typed this number, and then messaged us without
the "start my reminders <code>" text. They get asked whether to start their reminders.
"""
import logging
import re
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import session_factory
from app.core.guard import allow
from app.models import ChangeLog, ReminderMode, User, UserStatus, now
from app.providers.messaging import send_reply
from app.services import geo, signup
from app.services.reminders import DAY_BEFORE_TIMES, Reply, _clock, is_hi, parse_time, send_welcome

log = logging.getLogger(__name__)

P = "su:"  # every tap id in this flow starts with this, so it never clashes with Settings taps
MAX_LISTED = 9  # a WhatsApp list holds 10 rows: 9 addresses and "Not listed"

RESTART_WORDS = {"RESTART", "START OVER", "STARTOVER", "RESET"}
QUIT_WORDS = {"STOP", "CANCEL", "QUIT", "UNSUBSCRIBE"}


def _brand() -> str:
    return get_settings().brand_name


def _site() -> str:
    return re.sub(r"^https?://", "", get_settings().public_base_url).rstrip("/")


# ---- copy -------------------------------------------------------------------------------------

def greeting() -> str:
    return (f"Hi, I'm {_brand()}. I'll message you the evening before bin day with which bins to "
            "put out.\n\nWhat's your postcode?")


ASK_POSTCODE = "What's your postcode?"
CLAIM = ("Hi! I couldn't find a sign-up for this number. If you've just signed up on the website "
         "with a different number, send me the postcode you used and I'll find it. Otherwise, "
         "send your postcode and we'll set you up here.")
CLAIM_HOURS = 3  # how recent a website sign-up has to be to claim it by postcode
BAD_POSTCODE = "That doesn't look like a UK postcode. Try again, like E8 1AA."
LOOKUP_DOWN = "I couldn't look that postcode up just now. Try again in a minute."
ASK_HOUSE = "What's your house number or name?"
TOO_MANY = ("That's a lot of lookups in one go. Give it an hour and try again, or sign up at "
            "{site}.")
STILL_CHECKING = "Still reading your council's calendar, one moment."
QUIT = "OK, I've stopped. Message me any time to start again."
NO_CALENDAR = ("I couldn't get {council}'s calendar for that address automatically. You can set "
               "your bins up by hand at {site}, or send another postcode to try a different "
               "address.")


def _consent_text() -> str:
    s = get_settings()
    return (f"Last step. Starting means you agree to our terms ({s.terms_url}) and privacy "
            f"policy ({s.privacy_url}).\n\n"
            f"Now and then I'll also tell you about new {s.brand_name} features and other household "
            f"services within the {s.parent_name} family. Tap No feature updates if you'd rather "
            "not hear about those. "
            "Either button starts your reminders.")


CONSENT_BUTTONS = [(P + "go:yes", "Start reminders"), (P + "go:nomkt", "No feature updates")]


# ---- entry point ------------------------------------------------------------------------------

def wants(user: User | None) -> bool:
    """Whether this sender's message belongs to the sign-up rather than the usual commands:
    a number we don't know, or one whose reminders were never switched on."""
    return user is None or user.status == UserStatus.pending


def handle(db: Session, phone: str, text: str, user: User | None) -> str | None:
    """Reply to a message from someone not signed up yet. Sends the reply, commits, and
    returns its text (None when the message was ignored)."""
    t = text.strip()
    reaction = t.startswith(REACT)
    if reaction:
        # An emoji reaction to one of our messages. Only a yes or no on a question means
        # anything; anything else (or a reaction from a stranger) gets no reply.
        t = t[len(REACT):].strip()
        if user is None or user.wa_step not in ("bins", "off", "web") or not (_yes(t) or _no(t)):
            return None
    upper = " ".join(t.upper().split())
    if user is None:
        if upper in QUIT_WORDS or upper.startswith("DELETE"):
            return _send(db, phone, Reply("You're not signed up, so there's nothing to stop."))
        user = User(phone_e164=phone, referral_code=signup.new_referral_code())
        db.add(user)
        db.flush()
        if is_hi(t):  # the website's hi, but from a number nobody typed there
            user.wa_step = "claim"
            return _send(db, phone, Reply(CLAIM))
        if _looks_like_postcode(t):  # they led with it: skip the hello
            user.wa_step = "postcode"
            return _send(db, phone, _on_postcode(db, user, t))
        user.wa_step = "postcode"
        return _send(db, phone, Reply(greeting()))
    user.last_active_at = now()

    if upper in QUIT_WORDS:
        from app.services.retention import delete_account

        send_reply(db, phone, QUIT, kind="reply")
        delete_account(db, user)  # also removes the messages we sent them, this one included
        db.commit()
        return QUIT
    if user.wa_step == "checking":
        return _send(db, phone, Reply(STILL_CHECKING))
    if upper in RESTART_WORDS or t == P + "restart":
        return _send(db, phone, _restart(user))

    if _web_ready(user) and user.wa_step in (None, "web") and is_hi(t):
        return _switch_on(db, user)  # their hi: reminders on, welcome sent
    if user.wa_step is None:
        if _web_ready(user):  # started on the website, then messaged something else
            user.wa_step = "web"
            when = _clock(user.reminder_time)
            return _send(db, phone, Reply(
                "Looks like you started signing up on the website. Shall I start your "
                f"reminders? I'll message you the evening before each collection, at {when}.",
                buttons=[(P + "web:yes", "Start reminders"), (P + "web:over", "Start over")]))
        return _send(db, phone, _restart(user, hello=True))

    step = {"claim": _on_claim, "postcode": _on_postcode, "address": _on_address, "house": _on_house,
            "bins": _on_bins, "off": _on_bins, "time": _on_time, "consent": _on_consent,
            "web": _on_web}.get(user.wa_step)
    if step is None:
        return _send(db, phone, _restart(user))
    reply = step(db, user, t)
    if reply is None or isinstance(reply, str):  # None: sent separately; str: the welcome, sent
        return reply
    return _send(db, phone, reply)


def _send(db: Session, phone: str, reply: Reply, kind: str = "reply") -> str:
    send_reply(db, phone, reply.text, kind=kind, buttons=reply.buttons,
               list_button=reply.list_button, sections=reply.sections)
    db.commit()
    return reply.text


def _web_ready(user: User) -> bool:
    """A website sign-up that only lacks the hi: address, bins and consent are all done."""
    return bool(user.property_id and user.consent_at and user.bin_settings)


def _restart(user: User, hello: bool = False) -> Reply:
    user.wa_step, user.wa_data = "postcode", {}
    return Reply(greeting() if hello else f"No problem, let's start again. {ASK_POSTCODE}")


def _data(user: User) -> dict:
    return dict(user.wa_data or {})


def _save(user: User, **kw) -> None:
    d = _data(user)
    d.update(kw)
    user.wa_data = d  # a new dict, so SQLAlchemy sees the change


# ---- steps ------------------------------------------------------------------------------------

def _looks_like_postcode(t: str) -> bool:
    try:
        geo.normalise_postcode(t)
        return True
    except geo.BadPostcode:
        return False


def _on_claim(db: Session, user: User, t: str) -> Reply | str | None:
    """They sent the website's hi from a number nobody typed there (a typo, or another phone).
    One recent, finished website sign-up on the postcode they give: that's theirs, so it moves
    to this number and their reminders start. Otherwise it's a normal WhatsApp sign-up."""
    try:
        pc = geo.normalise_postcode(t)
    except geo.BadPostcode:
        return Reply(BAD_POSTCODE if re.search(r"\d", t) else CLAIM)
    from datetime import timedelta

    from app.models import Property

    since = now() - timedelta(hours=CLAIM_HOURS)
    found = [u for u in db.scalars(
        select(User).join(Property, User.property_id == Property.id)
        .where(Property.postcode == pc, User.status == UserStatus.pending, User.id != user.id,
               User.consent_at.is_not(None), User.hi_prompted_at >= since))
             if _web_ready(u)]
    if len(found) != 1:
        user.wa_step = "postcode"
        return _on_postcode(db, user, t)
    web = found[0]
    phone = user.phone_e164
    db.delete(user)  # the placeholder row for this number gives way to their real sign-up
    db.flush()
    web.phone_e164 = phone
    web.wa_step = None
    return _switch_on(db, web)


def _on_postcode(db: Session, user: User, t: str) -> Reply:
    try:
        pc = geo.normalise_postcode(t)
    except geo.BadPostcode:
        if len(t) > 12 or not re.search(r"\d", t):  # chat, not a mistyped postcode
            if len(t) > 12 and user.phone_e164:  # a question, maybe: the team sees it too
                from app.services import inbox

                db.commit()
                inbox.post(db, user.phone_e164, t, "signup")
            return Reply(f"Let's get you set up. {ASK_POSTCODE}")
        return Reply(BAD_POSTCODE)
    if not allow("wa-postcode", user.phone_e164 or user.id, 10, 3600):
        return Reply(TOO_MANY.format(site=_site()))
    try:
        info = geo.lookup_postcode(pc)
        hits = geo.lookup_addresses(pc)
    except geo.BadPostcode:
        return Reply(BAD_POSTCODE)
    except Exception:
        log.exception("wa sign-up: postcode lookup failed for %s", pc)
        return Reply(LOOKUP_DOWN)
    addresses = [{"uprn": h.uprn, "line": h.line, "house_number": h.house_number} for h in hits]
    user.wa_data = {"postcode": info.postcode, "council": info.district, "addresses": addresses}
    if not addresses:
        user.wa_step = "house"
        _save(user, manual=True)
        return Reply(f"Thanks. {ASK_HOUSE}")
    if len(addresses) > MAX_LISTED:
        user.wa_step = "house"
        return Reply(f"Thanks. {ASK_HOUSE}")
    user.wa_step = "address"
    return _address_list(addresses, list(range(len(addresses))))


def _tidy(line: str) -> str:
    """OS Places writes addresses in capitals: '10, ELM ROAD, LONDON, E8 1AA' → '10, Elm Road,
    London, E8 1AA'."""
    if not line.isupper():
        return line
    parts = [p.strip() for p in line.split(",")]
    return ", ".join(p if geo._PC.match(p.replace(" ", "")) else p.title() for p in parts)


def _short(line: str) -> str:
    """The street part of an address: drops the town and postcode at the end."""
    parts = [p.strip() for p in _tidy(line).split(",") if p.strip()]
    if len(parts) > 2:
        parts = parts[:-2]
    return ", ".join(parts)


def _address_list(addresses: list[dict], idx: list[int]) -> Reply:
    rows = [(f"{P}addr:{i}", _short(addresses[i]["line"])[:24], _tidy(addresses[i]["line"])[:72])
            for i in idx[:MAX_LISTED]]
    rows.append((P + "addr:none", "Not listed", "Type your house number or name"))
    return Reply("Which one is your home?", list_button="Choose address",
                 sections=[("Addresses", rows)])


def _matches(addresses: list[dict], q: str) -> list[int]:
    q = q.strip().lower()
    if not q:
        return []
    exact = [i for i, a in enumerate(addresses) if (a.get("house_number") or "").lower() == q]
    if exact:
        return exact
    pat = re.compile(r"(?<![\w])" + re.escape(q) + r"(?![\w])")
    return [i for i, a in enumerate(addresses) if pat.search(a["line"].lower())]


def _on_address(db: Session, user: User, t: str) -> Reply | None:
    d = _data(user)
    addresses = d.get("addresses", [])
    if t == P + "addr:none":
        user.wa_step = "house"
        _save(user, manual=True)
        return Reply(ASK_HOUSE)
    m = re.fullmatch(re.escape(P) + r"addr:(\d+)", t)
    if m and int(m.group(1)) < len(addresses):
        return _check(db, user, addresses[int(m.group(1))])
    if _looks_like_postcode(t):
        return _on_postcode(db, user, t)
    found = _matches(addresses, t)  # typed the number instead of tapping
    if len(found) == 1:
        return _check(db, user, addresses[found[0]])
    if found:
        return _address_list(addresses, found)
    return _address_list(addresses, list(range(len(addresses))))


def _on_house(db: Session, user: User, t: str) -> Reply | None:
    d = _data(user)
    if t.startswith(P) or not t:
        return Reply(ASK_HOUSE)
    if _looks_like_postcode(t) and not d.get("manual"):
        return _on_postcode(db, user, t)
    addresses = d.get("addresses", [])
    if not d.get("manual"):
        found = _matches(addresses, t)
        if len(found) == 1:
            return _check(db, user, addresses[found[0]])
        if found:
            user.wa_step = "address"
            return _address_list(addresses, found)
    # Not in the address list: go on what they typed, as the website does
    return _check(db, user, {"uprn": None, "house_number": t[:40],
                             "line": f"{t[:40]}, {d.get('postcode', '')}"})


# Council lookups can take a while, longer than Meta waits for the webhook to answer. So the
# check runs on a thread and its result arrives as a separate message. Tests and demo mode run
# it straight away.
def _check(db: Session, user: User, address: dict) -> Reply | None:
    if not allow("wa-check", user.phone_e164 or user.id, 4, 3600):
        return Reply(TOO_MANY.format(site=_site()))
    _save(user, address=address)
    user.wa_step = "checking"
    send_reply(db, user.phone_e164, "On it, checking…", kind="reply")
    db.commit()
    if get_settings().demo_mode:
        _run_check(user.id)
    else:
        threading.Thread(target=_run_check, args=(user.id,), daemon=True).start()
    return None


def _run_check(user_id: str) -> None:
    db = session_factory()()
    try:
        user = db.get(User, user_id)
        if user is None or user.wa_step != "checking":
            return
        d = _data(user)
        a = d.get("address") or {}
        try:
            res = signup.start_signup(db, d.get("postcode", ""), a.get("uprn"),
                                      a.get("house_number"), a.get("line"), user=user)
            signup.fill_gaps(db, user)
            db.commit()
        except Exception:
            log.exception("wa sign-up: council check failed for %s", user_id)
            db.rollback()
            user = db.get(User, user_id)
            if user is None:
                return
            res = None
        bins = signup.bins_out(db, user) if res and not res.error else []
        if not bins or not any(b.next for b in bins):
            user.wa_step = "postcode"
            council = (res.prop.council.name if res else None) or d.get("council") or "your council"
            _send(db, user.phone_e164, Reply(NO_CALENDAR.format(council=council, site=_site())))
            return
        user.wa_step = "bins"
        _send(db, user.phone_e164, _bins_reply(_short(a.get("line", "")), bins))
        if res and res.prop:
            signup.confirm_round(res.prop.id)  # already off the webhook's clock
    except Exception:
        log.exception("wa sign-up: check thread failed for %s", user_id)
        db.rollback()
    finally:
        db.close()


_FREQ = {"weekly": "every week", "fortnightly": "every 2 weeks", "three_weekly": "every 3 weeks",
         "four_weekly": "every 4 weeks"}


_DAYS = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
         "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"}


def _daily_words(days: list[str]) -> str:
    d = [x.upper()[:3] for x in days]
    if len(set(d)) == 7:
        return "Collected every day"
    if set(d) == {"MON", "TUE", "WED", "THU", "FRI"}:
        return "Collected every weekday"
    if set(d) == {"MON", "TUE", "WED", "THU", "FRI", "SAT"}:
        return "Collected Monday to Saturday"
    return "Collected most days"


def _bins_reply(where: str, bins) -> Reply:
    from datetime import date

    lines = []
    for b in bins:
        if not b.enabled:
            continue
        name = f"*{b.label}*"
        if b.colour and b.colour.lower() not in b.label.lower():
            name += f" ({b.colour})"
        days = " and ".join(_DAYS.get(d.upper()[:3], d) for d in b.days)
        freq = _FREQ.get(b.frequency, "")
        when = f"{freq} on {days}" if freq and days else (freq or (f"on {days}" if days else ""))
        when = when[:1].upper() + when[1:]
        if b.daily:  # never reminded about: say so, or they'll wonder where it went
            lines.append(f"{name}: {_daily_words(b.days)}, so no reminders for this one")
            continue
        nxt = date.fromisoformat(b.next).strftime("%a %-d %b") if b.next else ""
        lines.append(f"{name}: {when}" + (f". Next: {nxt}" if nxt else ""))
    head = f"Here's your bin calendar for {where}:" if where else "Here's your bin calendar:"
    return Reply(f"{head}\n\n" + "\n".join(lines) + "\n\nLooks right?",
                 buttons=[(P + "bins:ok", "Looks right"), (P + "bins:off", "Something's off")])


def _time_menu() -> Reply:
    rows = [(f"{P}time:{hhmm}", title, "") for hhmm, title in DAY_BEFORE_TIMES]
    return Reply("When should I nudge you? I'll message you the evening before each collection.",
                 list_button="Pick a time", sections=[("Evening before", rows)])


REACT = "react:"  # webhooks.message_text marks emoji reactions with this

_YES_EMOJI = {"\U0001F44D", "\U0001F44C", "\u2705", "\u2714"}  # thumbs up, OK hand, ticks
_NO_EMOJI = {"\U0001F44E", "\u274C", "\u2716"}  # thumbs down, crosses
_YES_WORDS = ("looks right", "looks good", "looks fine", "looks correct", "looks great",
              "all good", "all correct", "thats right", "thats correct", "correct", "right",
              "yes", "yeah", "yep", "yup", "ya", "y", "ok", "okay", "k", "perfect", "great",
              "spot on", "sure", "fine", "good", "carry on", "continue")
_NO_WORDS = ("somethings off", "something off", "something is off", "something wrong",
             "somethings wrong", "something is wrong", "not right", "not correct", "incorrect",
             "doesnt look right", "dont think so", "thats wrong", "wrong", "no", "nope", "nah",
             "n", "off")


def _emoji(t: str) -> str:
    """An emoji without skin tone or presentation marks: 👍🏽 → 👍."""
    return "".join(c for c in t.strip() if not ("\U0001F3FB" <= c <= "\U0001F3FF")
                   and c not in "\uFE0F\u200D")


def _words(t: str) -> str:
    return " ".join(re.sub(r"[^a-z ]", "", t.lower().replace("’", "").replace("'", "")).split())


def _starts(w: str, options) -> bool:
    return any(w == o or w.startswith(o + " ") for o in options)


def _no(t: str) -> bool:
    """'Something's off', 'nope', 'not right', 👎…"""
    return _emoji(t) in _NO_EMOJI or _starts(_words(t), _NO_WORDS)


def _yes(t: str) -> bool:
    """'Looks right', 'yep', 'looks good!', 👍 (any skin tone)…"""
    return (_emoji(t) in _YES_EMOJI or _starts(_words(t), _YES_WORDS)) and not _no(t)


def _on_bins(db: Session, user: User, t: str) -> Reply:
    if t == P + "bins:ok" or (not t.startswith(P) and _yes(t)):
        user.wa_step = "time"
        return _time_menu()
    if t == P + "bins:redo":
        return _restart(user)
    if user.wa_step == "off" and not t.startswith(P) and _no(t):
        return _restart(user)  # a second no: they want a different address
    if t == P + "bins:off" or (not t.startswith(P) and _no(t)):
        user.wa_step = "off"
        if user.property:  # for ops: which rounds people doubt at sign-up
            db.add(ChangeLog(scope="user", scope_id=user.id, kind="signup_off",
                             diff={"round_key": user.property.round_key}))
        return Reply("No problem. If I've got the wrong home, change the address. If the days "
                     "are off, carry on and tap Wrong collection day in Settings later: we'll "
                     "check with the council.",
                     buttons=[(P + "bins:redo", "Change address"), (P + "bins:ok", "Carry on")])
    if user.wa_step == "off":
        return Reply("Change the address, or carry on?",
                     buttons=[(P + "bins:redo", "Change address"), (P + "bins:ok", "Carry on")])
    return Reply("Does that look right?",
                 buttons=[(P + "bins:ok", "Looks right"), (P + "bins:off", "Something's off")])


def _on_time(db: Session, user: User, t: str) -> Reply:
    hhmm = t[len(P + "time:"):] if t.startswith(P + "time:") else parse_time(t, ReminderMode.night_before)
    if not hhmm or not re.fullmatch(r"\d{2}:\d{2}", hhmm):
        return _time_menu()
    user.reminder_mode, user.reminder_time = ReminderMode.night_before, hhmm
    user.wa_step = "consent"
    return Reply(_consent_text(), buttons=CONSENT_BUTTONS)


def _on_consent(db: Session, user: User, t: str) -> Reply | str:
    if t not in (P + "go:yes", P + "go:nomkt"):
        return Reply(_consent_text(), buttons=CONSENT_BUTTONS)
    user.consent_at = now()
    # "No feature updates" covers both: new feature news and the parent company's offers
    user.marketing_consent_at = now() if t == P + "go:yes" else None
    return _switch_on(db, user)


def _on_web(db: Session, user: User, t: str) -> Reply | str:
    if t == P + "web:yes" or (not t.startswith(P) and (_yes(t) or _words(t).startswith("start r"))):
        return _switch_on(db, user)
    if t == P + "web:over" or (not t.startswith(P) and _no(t)):
        return _restart(user)
    return Reply("Shall I start your reminders?",
                 buttons=[(P + "web:yes", "Start reminders"), (P + "web:over", "Start over")])


def _switch_on(db: Session, user: User) -> str:
    holder = db.scalar(select(User).where(User.phone_e164 == user.phone_e164, User.id != user.id))
    if holder is not None:  # can't happen (numbers are unique), but never two on one number
        holder.phone_e164 = None
    user.wa_step, user.wa_data = None, None
    signup.activate(db, user)
    text = send_welcome(db, user.phone_e164, user)  # its separate messages go out here
    db.commit()
    return text
