"""
Layer 3: reminders = council events + user overrides + user preferences.
Rebuilt from scratch for a user whenever any input changes (idempotent).
"""
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.manual import expand_rule
from app.core.config import get_settings
from app.models import (
    ChangeLog,
    CollectionEvent,
    Reminder,
    ReminderMode,
    ReminderStatus,
    User,
    UserBinSetting,
    UserStatus,
)
from app.providers.messaging import send_message, send_template_message

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
    return ("the day before each collection" if user.reminder_mode == ReminderMode.night_before
            else "on the morning of each collection")


def _user_bins(db: Session, user: User) -> list[UserBinSetting]:
    """The resident's bins in a fixed order, so BINS and OFF 2 always mean the same bin."""
    rows = db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == user.id)).all()
    rank = {c: i for i, c in enumerate(_CATEGORY_ORDER)}
    return sorted(rows, key=lambda s: (rank.get(s.category.value, 99), s.label.lower(), s.id))


def _bin_list(bins: list[UserBinSetting]) -> str:
    return "\n".join(f"{i}. {b.label} ({'on' if b.enabled else 'off'})" for i, b in enumerate(bins, 1))


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


_OFF_NOTE = " Reminders are off at the moment. Reply START to turn them back on."


def _set_time(db: Session, user: User, mode: ReminderMode, hhmm: str) -> str:
    user.reminder_mode, user.reminder_time = mode, hhmm
    rebuild_reminders(db, user)
    tail = _next_reminder(db, user) if user.status == UserStatus.active else _OFF_NOTE
    return f"Done. I'll message you {_when(user)} at {_clock(hhmm)}.{tail}"


def _settings_reply(db: Session, user: User) -> str:
    when = ("the day before" if user.reminder_mode == ReminderMode.night_before
            else "the morning of collection")
    lines = [f"Your {get_settings().brand_name} settings",
             f"Reminders: {when} at {_clock(user.reminder_time)}"
             + (" (off, reply START to turn them on)" if user.status != UserStatus.active else "")]
    bins = _user_bins(db, user)
    if bins:
        lines += ["", "Bins:", _bin_list(bins)]
    lines += ["", "To change them, send:",
              "TIME 6pm for a new time",
              "MORNING or DAY BEFORE to switch day",
              "OFF 2 or ON 2 to turn a bin off or on"]
    return "\n".join(lines)


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


def _toggle_bin(db: Session, user: User, on: bool, ref: str) -> str:
    word = "ON" if on else "OFF"
    bins = _user_bins(db, user)
    if not bins:
        return "You don't have any bins set up yet. Send SETTINGS to see your options."
    if not ref:
        return f"Send {word} with a bin number, like {word} 2. Send BINS to see the list."
    hits = _find_bin(bins, ref)
    if not hits:
        return "I couldn't find that bin. Send BINS to see the list."
    if len(hits) > 1:
        return f"Which one? Send {word} with its number:\n{_bin_list(bins)}"
    b = hits[0]
    n = bins.index(b) + 1
    if b.enabled == on:
        return f"{b.label} is already {'on' if on else 'off'}."
    b.enabled = on
    db.flush()
    rebuild_reminders(db, user)
    if on:
        tail = _next_reminder(db, user, b.label) if user.status == UserStatus.active else _OFF_NOTE
        return f"Done. {b.label} is back on.{tail}"
    if not any(x.enabled for x in bins):
        return (f"Done. {b.label} is off. That was your last bin, so you'll get no reminders "
                f"until you turn one on (ON {n}).")
    return f"Done. {b.label} is off, so I won't remind you about it. Send ON {n} to turn it back on."


def _command(db: Session, user: User, text: str) -> str | None:
    """Settings commands. Returns the reply, or None if the message isn't one of them."""
    words = text.strip().upper().split()
    if not words:
        return None
    head, rest = words[0], " ".join(words[1:])
    if head in ("SETTINGS", "MENU", "HELP", "CHANGE"):
        return _settings_reply(db, user)
    if head == "BINS":
        bins = _user_bins(db, user)
        if not bins:
            return "You don't have any bins set up yet."
        return f"Your bins:\n{_bin_list(bins)}\nSend OFF 2 or ON 2 to change one."
    if head in ("OFF", "ON"):
        return _toggle_bin(db, user, head == "ON", rest)
    if head == "TIME":
        hhmm = parse_time(rest, user.reminder_mode) if rest else None
        if not hhmm:
            return "I didn't catch that time. Try TIME 7pm or TIME 18:30."
        return _set_time(db, user, user.reminder_mode, hhmm)
    if head == "MORNING" or head in ("EVENING", "NIGHT") or words[:2] == ["DAY", "BEFORE"]:
        mode = ReminderMode.morning_of if head == "MORNING" else ReminderMode.night_before
        arg = " ".join(words[2:]) if words[:2] == ["DAY", "BEFORE"] else rest
        hhmm = parse_time(arg, mode) if arg else _DEFAULT_TIME[mode]
        if not hhmm:
            example = "MORNING 6:30am" if mode == ReminderMode.morning_of else "DAY BEFORE 7pm"
            return f"I didn't catch that time. Try {example}."
        return _set_time(db, user, mode, hhmm)
    return None


def handle_inbound(db: Session, phone: str, text: str) -> str:
    """Replies to a user's message: DONE / SNOOZE / WRONG / STOP / START, and the settings
    commands (SETTINGS, TIME, MORNING, DAY BEFORE, BINS, ON, OFF). Returns what we answered."""
    user = db.scalar(select(User).where(User.phone_e164 == phone))
    if not user:
        site = re.sub(r"^https?://", "", get_settings().public_base_url).rstrip("/")
        return f"Hi! Sign up at {site} to get bin reminders."
    cmd = text.strip().upper().split()[0] if text.strip() else ""
    last = db.scalar(
        select(Reminder).where(Reminder.user_id == user.id, Reminder.status == ReminderStatus.sent)
        .order_by(Reminder.sent_at.desc())
    )
    settings_reply = None if cmd in ("STOP", "START", "DONE", "SNOOZE", "WRONG") else _command(db, user, text)
    if settings_reply is not None:
        reply = settings_reply
    elif cmd == "STOP":
        user.status = UserStatus.stopped
        rebuild_reminders(db, user)
        reply = "Reminders are off. Reply START to turn them back on."
    elif cmd == "START":
        user.status = UserStatus.active
        rebuild_reminders(db, user)
        reply = "Reminders are back on."
    elif cmd == "DONE" and last:
        last.status = ReminderStatus.done
        last.reply = "done"
        reply = "Nice one. I'll be in touch before the next one."
    elif cmd == "SNOOZE" and last:
        last.status = ReminderStatus.snoozed
        last.reply = "snooze"
        db.add(Reminder(user_id=user.id, collection_date=last.collection_date, bins=last.bins,
                        send_at=datetime.now(ZoneInfo("UTC")) + timedelta(hours=2)))
        reply = "No problem, I'll nudge you again in 2 hours."
    elif cmd == "WRONG" and last:
        # A vote against the round's calendar: the monitor's dispute job (split.py) asks the
        # council about more homes on the street and splits the round if it's really two.
        last.reply = "wrong"
        if user.property:
            db.add(ChangeLog(scope="round", scope_id=user.property.round_key, kind="wrong_day",
                             diff={"user_id": user.id,
                                   "collection_date": last.collection_date.isoformat()}))
        # No promise to get back to them: nothing is sent unless their round actually changes.
        reply = ("Thanks, noted. If your home's days are different, tap \"These aren't my "
                 f"days\" in {get_settings().brand_name} to set your own.")
    else:
        reply = "Reply DONE, SNOOZE, WRONG or STOP. Send SETTINGS to change your reminder time or bins."
    send_message(db, phone, reply, kind="reply")
    db.commit()
    return reply
