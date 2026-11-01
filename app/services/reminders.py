"""
Layer 3: reminders = council events + user overrides + user preferences.
Rebuilt from scratch for a user whenever any input changes (idempotent).
"""
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
from app.providers.messaging import send_message

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
        row = send_message(db, user.phone_e164, render_reminder(user, r), kind="reminder")
        if row.error:
            r.status = ReminderStatus.failed
        else:
            r.status = ReminderStatus.sent
            r.sent_at = now_utc
            r.provider_message_id = row.provider_message_id
            sent += 1
    db.commit()
    return sent


def handle_inbound(db: Session, phone: str, text: str) -> str:
    """Replies to a user's message (DONE / SNOOZE / WRONG / STOP). Returns what we answered."""
    user = db.scalar(select(User).where(User.phone_e164 == phone))
    if not user:
        return "Hi! Sign up at binbot.app to get bin reminders."
    cmd = text.strip().upper().split()[0] if text.strip() else ""
    last = db.scalar(
        select(Reminder).where(Reminder.user_id == user.id, Reminder.status == ReminderStatus.sent)
        .order_by(Reminder.sent_at.desc())
    )
    if cmd == "STOP":
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
                 "days\" in BinBot to set your own.")
    else:
        reply = "Reply DONE, SNOOZE, WRONG or STOP."
    send_message(db, phone, reply, kind="reply")
    db.commit()
    return reply
