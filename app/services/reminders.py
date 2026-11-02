"""
Layer 3: reminders = council events + user overrides + user preferences.
Rebuilt from scratch for a user whenever any input changes (idempotent).
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.manual import expand_rule
from app.core.config import get_settings
from app.models import (
    LEVELS,
    ChangeLog,
    CollectionEvent,
    Reminder,
    ReminderMode,
    ReminderStatus,
    User,
    UserBinSetting,
    UserStatus,
)
from app.providers.messaging import send_reply, send_template_message

_CATEGORY_NAMES = {
    "recycling": "Recycling", "general": "General waste", "food": "Food waste",
    "garden": "Garden", "glass": "Glass", "paper": "Paper and card", "other": "Bin",
}


def effective_collections(db: Session, user: User) -> dict[date, list[dict]]:
    """Dates -> bins, after applying the user's enable/disable and manual rules."""
    today = date.today()
    horizon = get_settings().horizon_days
    out: dict[date, list[dict]] = {}
    settings = db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == user.id)).all()
    for s in settings:
        if not s.enabled:
            continue
        bin_info = {"label": s.label, "category": s.category.value, "setting_id": s.id}
        if s.override_rule:
            dates = expand_rule(s.override_rule, today, horizon)
        elif s.bin_type_id and user.property:
            dates = db.scalars(
                select(CollectionEvent.date).where(
                    CollectionEvent.round_key == user.property.round_key,
                    CollectionEvent.bin_type_id == s.bin_type_id,
                    CollectionEvent.date >= today,
                )
            ).all()
        else:
            dates = []
        for d in dates:
            out.setdefault(d, []).append(bin_info)
    return dict(sorted(out.items()))


def send_at_for(user: User, collection: date) -> datetime:
    tz = ZoneInfo(user.timezone or "Europe/London")
    hh, mm = (int(x) for x in user.reminder_time.split(":"))
    day = collection - timedelta(days=1) if user.reminder_mode == ReminderMode.night_before else collection
    return datetime.combine(day, time(hh, mm), tzinfo=tz)


def rebuild_reminders(db: Session, user: User) -> int:
    """Drop unsent reminders for the user and recreate from current data. Returns count."""
    for r in db.scalars(
        select(Reminder).where(Reminder.user_id == user.id,
                               Reminder.status == ReminderStatus.scheduled)
    ):
        db.delete(r)
    db.flush()
    if user.status != UserStatus.active or not user.phone_e164:
        return 0
    n = 0
    now_ = datetime.now(ZoneInfo(user.timezone or "Europe/London"))
    for d, bins in effective_collections(db, user).items():
        send_at = send_at_for(user, d)
        if send_at < now_:
            continue
        db.add(Reminder(user_id=user.id, collection_date=d, send_at=send_at,
                        bins=[{"label": b["label"], "category": b["category"]} for b in bins]))
        n += 1
    db.flush()
    return n


def render_reminder(user: User, r: Reminder) -> str:
    names = [b["label"] for b in r.bins]
    when = "tomorrow" if user.reminder_mode == ReminderMode.night_before else "today"
    if len(names) == 1:
        head = f"{names[0]} goes out {when}"
    else:
        head = f"{', '.join(names[:-1])} and {names[-1]} go out {when}"
    day = r.collection_date.strftime("%a %-d %b")
    return (f"{head} ({day}).\nReply DONE when it's out, SNOOZE for a nudge later, or WRONG if "
            f"that's not your bin day. STOP to stop.")


def dispatch_due(db: Session, now_utc: datetime | None = None) -> int:
    """Send every reminder whose time has come. Run every minute by the worker."""
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    due = db.scalars(
        select(Reminder).where(Reminder.status == ReminderStatus.scheduled,
                               Reminder.send_at <= now_utc)
    ).all()
    sent = 0
    for r in due:
        user = db.get(User, r.user_id)
        if user.status != UserStatus.active:
            r.status = ReminderStatus.cancelled
            continue
        when = "tomorrow" if user.reminder_mode == ReminderMode.night_before else "today"
        row = send_template_message(
            db, user.phone_e164, get_settings().meta_reminder_template,
            [_join([b["label"] for b in r.bins]), when, r.collection_date.strftime("%a %-d %b")],
            render_reminder(user, r), kind="reminder", buttons=True)
        if row.error:
            r.status = ReminderStatus.failed
        else:
            r.status = ReminderStatus.sent
            r.sent_at = now_utc
            r.provider_message_id = row.provider_message_id
            sent += 1
    db.commit()
    return sent


def welcome_message(db: Session, user: User) -> str:
    """First message, sent the moment reminders go on, so the resident sees the service works
    (and, on WhatsApp, it opens the 24-hour window for free-form replies)."""
    first = db.scalar(select(Reminder).where(Reminder.user_id == user.id)
                      .order_by(Reminder.send_at))
    # "night_before" is the day before at any time the resident picks (4pm is an option).
    when = ("the day before each collection" if user.reminder_mode == ReminderMode.night_before
            else "on the morning of each collection")
    hh, mm = (int(x) for x in user.reminder_time.split(":"))
    clock = f"{hh % 12 or 12}{f':{mm:02d}' if mm else ''}{'pm' if hh >= 12 else 'am'}"
    msg = f"{get_settings().brand_name} here. You're set up: I'll message you {when}, at {clock}."
    if first:
        names = [b["label"] for b in first.bins]
        bins = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
        msg += f"\nFirst up: {bins} on {first.collection_date.strftime('%a %-d %b')}."
    msg += "\nReply STOP any time to turn reminders off."
    # Invite link, same format as the referral API: people forward it to neighbours. The level
    # names and the top threshold come from LEVELS, so the copy follows any change there.
    link = f"{get_settings().public_base_url}/?ref={user.referral_code}"
    top_at, top_name = LEVELS[-1]
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
             8: "eight", 9: "nine", 10: "ten"}
    msg += (f"\n\nYou're a {LEVELS[0][1]} for now. Every neighbour who signs up with your link "
            f"levels you up, and {words.get(top_at, str(top_at))} makes you a certified "
            f"{top_name.lower()}: {link}")
    return msg[:4096]  # WhatsApp's text limit; the welcome is far shorter in practice


def start_link(code: str) -> str:
    """wa.me link that opens WhatsApp with "Hi <brand>, start my reminders <code>" ready to send."""
    s = get_settings()
    digits = re.sub(r"\D", "", s.meta_display_number)
    return f"https://wa.me/{digits}?text={quote(f'Hi {s.brand_name}, start my reminders {code}')}"


_START_RE = re.compile(r"\bstart my reminders\s+([A-Za-z0-9]{6})\b", re.IGNORECASE)


def _handle_hi(db: Session, phone: str, text: str) -> tuple[str, str] | None:
    """'Hi <brand>, start my reminders <code>': the resident proving the number is theirs.
    Turns their reminders on and sends the welcome (free text: they've just messaged us).
    If they wrote from a different number than they typed, the one they wrote from wins."""
    m = _START_RE.search(text)
    if not m:
        return None
    user = db.scalar(select(User).where(User.start_code == m.group(1).upper()))
    if not user:
        sender = db.scalar(select(User).where(User.phone_e164 == phone))
        if sender and sender.status == UserStatus.active:
            return 'Your reminders are already on. Message "settings" to change them.', "reply"
        return None
    holder = db.scalar(select(User).where(User.phone_e164 == phone, User.id != user.id))
    if holder:  # an older sign-up on this number: the person holding the phone moves over
        holder.phone_e164 = None
        holder.status = UserStatus.stopped
        rebuild_reminders(db, holder)
        db.flush()
    user.phone_e164 = phone
    user.start_code = None
    user.status = UserStatus.active
    rebuild_reminders(db, user)
    return welcome_message(db, user), "welcome"


# ---- WhatsApp commands: change reminder time and bins -------------------------------------------

_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*(AM|PM)?$")
_CATEGORY_ORDER = list(_CATEGORY_NAMES)
_DEFAULT_TIME = {ReminderMode.night_before: "19:00", ReminderMode.morning_of: "06:30"}


def _clock(hhmm: str) -> str:
    """'19:00' -> '7pm', '06:30' -> '6:30am'."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    return f"{hh % 12 or 12}{f':{mm:02d}' if mm else ''}{'pm' if hh >= 12 else 'am'}"


def parse_time(raw: str, mode: ReminderMode) -> str | None:
    """'4pm', '16:00', '6.30am', '7' -> 'HH:MM', or None if it isn't a time.
    A bare hour follows the reminder mode: 7 is 7pm the day before, 7am on the morning.
    A zero-padded or 24-hour time (07:00, 16:00) is taken as written."""
    m = _TIME_RE.match(raw.strip().upper().replace(" ", ""))
    if not m:
        return None
    hh, mm, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if mm > 59:
        return None
    if ampm:
        if not 1 <= hh <= 12:
            return None
        hh = hh % 12 + (12 if ampm == "PM" else 0)
    elif hh > 23:
        return None
    elif 1 <= hh <= 11 and not raw.strip().startswith("0"):
        if mode == ReminderMode.night_before:
            hh += 12
    return f"{hh:02d}:{mm:02d}"


def _when(user: User) -> str:
    return ("the day before" if user.reminder_mode == ReminderMode.night_before
            else "on the morning of collection")


@dataclass
class Reply:
    """A reply to the resident: text, optionally with reply buttons (id, title) or a list menu
    (button label + sections of (id, title, description) rows)."""
    text: str
    buttons: list = field(default_factory=list)
    list_button: str = ""
    sections: list = field(default_factory=list)


SETTINGS_BUTTON = ("menu:settings", "Settings")
# The times offered in the menu: (id, title). Typed TIME 5:45pm still works for anything else.
DAY_BEFORE_TIMES = [("16:00", "4pm"), ("18:00", "6pm"), ("19:00", "7pm"), ("20:00", "8pm"), ("21:00", "9pm")]
MORNING_TIMES = [("06:00", "6am"), ("06:30", "6:30am"), ("07:00", "7am")]


def _user_bins(db: Session, user: User) -> list[UserBinSetting]:
    """The resident's bins in a fixed order."""
    rows = db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == user.id)).all()
    rank = {c: i for i, c in enumerate(_CATEGORY_ORDER)}
    return sorted(rows, key=lambda s: (rank.get(s.category.value, 99), s.label.lower(), s.id))


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _next_reminder(db: Session, user: User, label: str | None = None) -> str:
    """' Next: Recycling on Wed 4 Nov.' from the rebuilt schedule ('' if nothing is due)."""
    for r in db.scalars(select(Reminder).where(Reminder.user_id == user.id,
                                               Reminder.status == ReminderStatus.scheduled)
                        .order_by(Reminder.send_at)):
        names = [b["label"] for b in r.bins]
        if label is None or label in names:
            return f" Next: {_join(names)} on {r.collection_date.strftime('%a %-d %b')}."
    return ""


_OFF_NOTE = " Reminders are paused at the moment. Reply START to turn them back on."


# ---- menus -------------------------------------------------------------------------------------

def settings_menu(db: Session, user: User) -> Reply:
    on = [b.label for b in _user_bins(db, user) if b.enabled]
    paused = user.status != UserStatus.active
    text = (f"Your reminders: {_when(user)} at {_clock(user.reminder_time)}"
            + (" (paused)" if paused else "") + ".\n"
            + f"Bins on: {', '.join(on) if on else 'none'}.\n"
            + "What would you like to change?")
    last = ("menu:resume", "Turn reminders on") if paused else ("menu:pause", "Pause reminders")
    return Reply(text, buttons=[("menu:time", "Reminder time"), ("menu:bins", "My bins"), last])


def time_menu(user: User) -> Reply:
    def rows(times, prefix, mode):
        return [(f"{prefix}:{hhmm}", title,
                 "Current" if user.reminder_mode == mode and user.reminder_time == hhmm else "")
                for hhmm, title in times]
    return Reply("Pick a time for your reminders.", list_button="Pick a time", sections=[
        ("Day before", rows(DAY_BEFORE_TIMES, "time", ReminderMode.night_before)),
        ("Morning of", rows(MORNING_TIMES, "time_m", ReminderMode.morning_of))])


def bins_menu(db: Session, user: User) -> Reply:
    bins = _user_bins(db, user)
    if not bins:
        return Reply("You don't have any bins set up yet.", buttons=[SETTINGS_BUTTON])
    rows = [(f"bin:{b.id}", b.label[:24], "On, tap to turn off" if b.enabled else "Off, tap to turn on")
            for b in bins[:10]]
    return Reply("Tap a bin to turn its reminders on or off.", list_button="Your bins",
                 sections=[("Your bins", rows)])


def _set_time(db: Session, user: User, mode: ReminderMode, hhmm: str) -> Reply:
    user.reminder_mode, user.reminder_time = mode, hhmm
    rebuild_reminders(db, user)
    tail = _next_reminder(db, user) if user.status == UserStatus.active else _OFF_NOTE
    return Reply(f"Done. I'll remind you {_when(user)} at {_clock(hhmm)}.{tail}")


def _set_bin(db: Session, user: User, b: UserBinSetting, on: bool) -> Reply:
    after = [("menu:bins", "My bins"), ("menu:done", "Done")]
    if b.enabled == on:
        return Reply(f"{b.label} is already {'on' if on else 'off'}.", buttons=after)
    b.enabled = on
    db.flush()
    rebuild_reminders(db, user)
    if on:
        tail = _next_reminder(db, user, b.label) if user.status == UserStatus.active else _OFF_NOTE
        return Reply(f"{b.label} is back on.{tail}", buttons=after)
    if not any(x.enabled for x in _user_bins(db, user)):
        return Reply(f"{b.label} is off. That was your last bin, so you'll get no reminders until "
                     "you turn one back on.", buttons=after)
    return Reply(f"{b.label} is off. I won't remind you about it.", buttons=after)


def _pause(db: Session, user: User) -> Reply:
    user.status = UserStatus.stopped
    rebuild_reminders(db, user)
    return Reply("Reminders are off. Reply START or tap Turn reminders on when you want them back.",
                 buttons=[("menu:resume", "Turn reminders on")])


def _resume(db: Session, user: User) -> Reply:
    user.status = UserStatus.active
    rebuild_reminders(db, user)
    return Reply(f"Reminders are back on.{_next_reminder(db, user)}")


def _find_bin(bins: list[UserBinSetting], ref: str) -> list[UserBinSetting]:
    ref = ref.strip().lower()
    if ref.isdigit():
        n = int(ref)
        return [bins[n - 1]] if 1 <= n <= len(bins) else []
    for test in (lambda b: b.label.lower() == ref,
                 lambda b: ref in b.label.lower(),
                 lambda b: ref in b.category.value or ref in _CATEGORY_NAMES[b.category.value].lower()):
        hits = [b for b in bins if test(b)]
        if hits:
            return hits
    return []


def _tap(db: Session, user: User, tap: str) -> Reply | None:
    """A tapped button or list row: stable ids, never trusted blindly (bins must be the sender's)."""
    kind, _, arg = tap.partition(":")
    if kind == "menu":
        return {"settings": lambda: settings_menu(db, user), "time": lambda: time_menu(user),
                "bins": lambda: bins_menu(db, user), "pause": lambda: _pause(db, user),
                "resume": lambda: _resume(db, user),
                "done": lambda: Reply("All set.")}.get(arg, lambda: settings_menu(db, user))()
    if kind in ("time", "time_m"):
        mode = ReminderMode.night_before if kind == "time" else ReminderMode.morning_of
        offered = DAY_BEFORE_TIMES if kind == "time" else MORNING_TIMES
        if arg not in dict(offered):
            return time_menu(user)
        return _set_time(db, user, mode, arg)
    if kind == "bin":
        b = db.get(UserBinSetting, arg)
        if b is None or b.user_id != user.id:
            return bins_menu(db, user)
        return _set_bin(db, user, b, not b.enabled)
    return None


_TAP_RE = re.compile(r"^(menu|time|time_m|bin):[A-Za-z0-9:_]+$")
# "turn off garden", "switch the garden bin off", "no garden", "stop garden", "turn on garden"
_PHRASE_RES = [
    (re.compile(r"^(?:turn|switch)\s+(off|on)\s+(?:the\s+|my\s+)?(.+)$", re.IGNORECASE), None),
    (re.compile(r"^(?:turn|switch)\s+(?:the\s+|my\s+)?(.+?)\s+(off|on)$", re.IGNORECASE), "swap"),
    (re.compile(r"^(no|stop|off|on)\s+(?:the\s+|my\s+)?(.+)$", re.IGNORECASE), None),
]


def _bin_phrase(db: Session, user: User, text: str) -> Reply | None:
    t = re.sub(r"[.!?]+$", "", text.strip())
    for rx, order in _PHRASE_RES:
        m = rx.match(t)
        if not m:
            continue
        word, ref = (m.group(2), m.group(1)) if order == "swap" else (m.group(1), m.group(2))
        on = word.lower() == "on"
        ref = re.sub(r"\s+(bin|bins|caddy|box)$", "", ref.strip(), flags=re.IGNORECASE)
        bins = _user_bins(db, user)
        hits = _find_bin(bins, ref) if bins else []
        if len(hits) == 1:
            return _set_bin(db, user, hits[0], on)
        if order is None and rx is _PHRASE_RES[2][0] and not hits:
            return None  # "no thanks", "stop please": not about a bin (STOP still works)
        return bins_menu(db, user)
    return None


def _command(db: Session, user: User, text: str) -> Reply | None:
    """Typed shortcuts and taps. Returns the reply, or None if the message isn't one of them."""
    t = text.strip()
    if _TAP_RE.match(t):
        return _tap(db, user, t)
    words = t.upper().split()
    if not words:
        return None
    head, rest = words[0], " ".join(words[1:])
    if head in ("SETTINGS", "MENU", "HELP", "CHANGE") and len(words) == 1:
        return settings_menu(db, user)
    if head == "BINS" and len(words) == 1:  # hidden alias
        return bins_menu(db, user)
    if head in ("OFF", "ON") and rest:  # hidden aliases: OFF 2, ON garden
        bins = _user_bins(db, user)
        hits = _find_bin(bins, rest)
        return _set_bin(db, user, hits[0], head == "ON") if len(hits) == 1 else bins_menu(db, user)
    if head == "TIME":
        hhmm = parse_time(rest, user.reminder_mode) if rest else None
        if not hhmm:
            return Reply("I didn't catch that time. Try TIME 7pm or TIME 18:30, or pick one below.",
                         list_button="Pick a time", sections=time_menu(user).sections)
        return _set_time(db, user, user.reminder_mode, hhmm)
    if head == "MORNING" or head in ("EVENING", "NIGHT") or words[:2] == ["DAY", "BEFORE"]:
        mode = ReminderMode.morning_of if head == "MORNING" else ReminderMode.night_before
        arg = " ".join(words[2:]) if words[:2] == ["DAY", "BEFORE"] else rest
        hhmm = parse_time(arg, mode) if arg else _DEFAULT_TIME[mode]
        if not hhmm:
            example = "MORNING 6:30am" if mode == ReminderMode.morning_of else "DAY BEFORE 7pm"
            return Reply(f"I didn't catch that time. Try {example}.")
        return _set_time(db, user, mode, hhmm)
    return _bin_phrase(db, user, t)


FALLBACK = "Reply DONE, SNOOZE or WRONG to your last reminder, or tap below to change your settings."


def handle_inbound(db: Session, phone: str, text: str) -> str:
    """Replies to a user's message or tap: the sign-up hi ("start my reminders <code>"), DONE /
    SNOOZE / WRONG / STOP / START, the settings menus (taps, or "settings"), and typed shortcuts
    (TIME 4pm, MORNING, DAY BEFORE, "turn off garden"). Returns the reply text."""
    hi = _handle_hi(db, phone, text)
    if hi:
        send_reply(db, phone, hi[0], kind=hi[1])
        db.commit()
        return hi[0]
    user = db.scalar(select(User).where(User.phone_e164 == phone))
    if not user:
        site = re.sub(r"^https?://", "", get_settings().public_base_url).rstrip("/")
        return f"Hi! Sign up at {site} to get bin reminders."
    words = text.strip().upper().split()
    cmd = words[0] if words else ""
    last = db.scalar(
        select(Reminder).where(Reminder.user_id == user.id, Reminder.status == ReminderStatus.sent)
        .order_by(Reminder.sent_at.desc())
    )
    reply = None
    if cmd not in ("STOP", "START", "DONE", "SNOOZE", "WRONG") or len(words) > 1:
        reply = _command(db, user, text)
    if reply is not None:
        pass
    elif cmd == "STOP":
        reply = _pause(db, user)
    elif cmd == "START":
        reply = _resume(db, user)
    elif cmd == "DONE" and last:
        last.status = ReminderStatus.done
        last.reply = "done"
        reply = Reply("Nice one. I'll be in touch before the next one.")
    elif cmd == "SNOOZE" and last:
        last.status = ReminderStatus.snoozed
        last.reply = "snooze"
        db.add(Reminder(user_id=user.id, collection_date=last.collection_date, bins=last.bins,
                        send_at=datetime.now(ZoneInfo("UTC")) + timedelta(hours=2)))
        reply = Reply("No problem, I'll nudge you again in 2 hours.")
    elif cmd == "WRONG" and last:
        # A vote against the round's calendar: the monitor's dispute job (split.py) asks the
        # council about more homes on the street and splits the round if it's really two.
        last.reply = "wrong"
        if user.property:
            db.add(ChangeLog(scope="round", scope_id=user.property.round_key, kind="wrong_day",
                             diff={"user_id": user.id,
                                   "collection_date": last.collection_date.isoformat()}))
        # No promise to get back to them: nothing is sent unless their round actually changes.
        reply = Reply("Thanks, noted. If your home's days are different, tap \"These aren't my "
                      f"days\" in {get_settings().brand_name} to set your own.")
    else:
        reply = Reply(FALLBACK, buttons=[SETTINGS_BUTTON])
    send_reply(db, phone, reply.text, kind="reply", buttons=reply.buttons,
               list_button=reply.list_button, sections=reply.sections)
    db.commit()
    return reply.text
