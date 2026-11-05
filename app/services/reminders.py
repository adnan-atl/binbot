"""
Layer 3: reminders = council events + user overrides + user preferences.
Rebuilt from scratch for a user whenever any input changes (idempotent).
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.manual import expand_rule
from app.core.config import get_settings
from app.models import (
    BINFLUENCER_AT,
    Referral,
    ChangeLog,
    CollectionEvent,
    Reminder,
    ReminderMode,
    ReminderStatus,
    User,
    UserBinSetting,
    UserStatus,
)
from app.services.normalize import describe_rule, is_daily
from app.providers.messaging import get_provider, send_message, send_reply, send_template_batch, uses_template

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
        if is_daily(describe_rule(list(dates))["days"]):
            continue  # collected (nearly) every day: a reminder every evening would be noise
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


# ---- referrals: credit on activation, news with the next reminder -------------------------------

def credit_referral(db: Session, user: User) -> None:
    """The friend now gets reminders, so they count for whoever invited them (once). Called when
    reminders go on: a sign-up that never finishes doesn't count."""
    ref_id = user.referred_by_id
    if not ref_id or ref_id == user.id or not db.get(User, ref_id):
        return
    if db.scalar(select(Referral.id).where(Referral.referrer_id == ref_id,
                                           Referral.referred_id == user.id)):
        return
    db.add(Referral(referrer_id=ref_id, referred_id=user.id))
    db.flush()


def referral_news(db: Session, user: User) -> tuple[str, list[Referral]] | None:
    """One line about everyone who joined with the user's link since they were last told, or
    None. Batched on purpose: it rides on the next reminder, never a message of its own."""
    rows = db.scalars(select(Referral).where(Referral.referrer_id == user.id,
                                             Referral.announced_at.is_(None))).all()
    if not rows:
        return None
    total = db.scalar(select(func.count()).select_from(Referral)
                      .where(Referral.referrer_id == user.id)) or 0
    n = len(rows)
    who = "A neighbour" if n == 1 else f"{n} neighbours"
    line = f"\U0001F389 {who} joined with your link."
    if total < BINFLUENCER_AT:
        left = BINFLUENCER_AT - total
        line += f" {left} more and you're officially a Binfluencer."
    elif total - n < BINFLUENCER_AT:  # this batch took them to it
        line += f" That's {BINFLUENCER_AT}: you're officially a Binfluencer!"
    else:
        line += f" That's {total} neighbours so far."
    return line, rows


def _mark_announced(rows: list[Referral], when: datetime) -> None:
    for row in rows:
        row.announced_at = when


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
    """Send every reminder whose time has come. Run every minute by the worker. The sends go out
    in parallel (send_template_batch), so a few thousand people on the same time all get theirs
    within a minute or two. Referral news rides along when it can (referral_news)."""
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    s = get_settings()
    due = db.scalars(
        select(Reminder).where(Reminder.status == ReminderStatus.scheduled,
                               Reminder.send_at <= now_utc)
    ).all()
    news_template = s.meta_reminder_news_template if uses_template(s.meta_reminder_news_template) else ""
    free_text = not uses_template(s.meta_reminder_template)
    plain, with_news = [], []  # (reminder, job, news rows)
    for r in due:
        user = db.get(User, r.user_id)
        if user.status != UserStatus.active:
            r.status = ReminderStatus.cancelled
            continue
        when = "tomorrow" if user.reminder_mode == ReminderMode.night_before else "today"
        params = [_join([b["label"] for b in r.bins]), when, r.collection_date.strftime("%a %-d %b")]
        body = render_reminder(user, r)
        news = referral_news(db, user) if (news_template or free_text) else None
        if news:
            with_news.append((r, (user.phone_e164, params + [news[0]], f"{body}\n\n{news[0]}"), news[1]))
        else:
            plain.append((r, (user.phone_e164, params, body), None))
    rows = send_template_batch(db, s.meta_reminder_template, [j for _, j, _ in plain],
                               kind="reminder", buttons=True)
    if with_news:
        rows += send_template_batch(db, news_template or s.meta_reminder_template,
                                    [j for _, j, _ in with_news], kind="reminder", buttons=True)
    sent = 0
    for (r, _, news_rows), row in zip(plain + with_news, rows):
        if row.error:
            r.status = ReminderStatus.failed
        else:
            r.status = ReminderStatus.sent
            r.sent_at = now_utc
            r.provider_message_id = row.provider_message_id
            db.get(User, r.user_id).last_active_at = now_utc  # a reminder that reaches them is use
            if news_rows:
                _mark_announced(news_rows, now_utc)
            sent += 1
    db.commit()
    return sent


# ---- "share with neighbours": once, after they've had their first reminder --------------------

def share_link(user: User) -> str:
    return f"{get_settings().public_base_url}/?ref={user.referral_code}"


def share_text(user: User) -> str:
    return (f"If you want to share {get_settings().brand_name} with neighbours or friends, send "
            f"them this link: {share_link(user)}")


def _wants_share(user: User) -> bool:
    """One nudge per person, and only to people who said yes to hearing from us (it's promoting
    the service, so the opt-out at sign-up covers it)."""
    return (user.status == UserStatus.active and user.share_nudged_at is None
            and user.marketing_consent_at is not None and bool(user.phone_e164))


def share_after_done(db: Session, user: User, phone: str) -> bool:
    """They just tapped Done on a reminder: the product worked, and the chat window is open,
    so the nudge goes now as free text (no template, no charge)."""
    if not _wants_share(user):
        return False
    send_message(db, phone, share_text(user), kind="share")
    user.share_nudged_at = datetime.now(ZoneInfo("UTC"))
    return True


def share_nudges(db: Session, now_utc: datetime | None = None) -> int:
    """Daily (late morning): the share nudge for everyone whose first reminded collection was
    yesterday or before and who hasn't had it (didn't tap Done). Goes as the
    META_SHARE_TEMPLATE template; without one (Meta still reviewing it) nobody is sent anything
    and they stay in the queue. Returns how many were sent."""
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    s = get_settings()
    can_template = uses_template(s.meta_share_template)
    if not can_template and hasattr(get_provider(), "send_template"):
        return 0  # real WhatsApp but no approved template: free text wouldn't arrive
    today = now_utc.astimezone(ZoneInfo(s.timezone)).date()
    reminded = (select(Reminder.user_id)
                .where(Reminder.status.in_([ReminderStatus.sent, ReminderStatus.done,
                                            ReminderStatus.snoozed]),
                       Reminder.collection_date < today))
    users = [u for u in db.scalars(select(User).where(User.id.in_(reminded))) if _wants_share(u)]
    jobs = [(u.phone_e164, [s.brand_name, share_link(u)],
             f"Hope that made bin day easier. {share_text(u)} \U0001F517") for u in users]
    rows = send_template_batch(db, s.meta_share_template, jobs, kind="share")
    sent = 0
    for u, row in zip(users, rows):
        if not row.error:
            u.share_nudged_at = now_utc
            sent += 1
    db.commit()
    return sent


# ---- website sign-ups that never sent their hi -------------------------------------------------

SIGNUP_NUDGE_MINUTES = 12
SIGNUP_NUDGE_TEXT = ("Your bin reminders are nearly ready. Tap Start reminders to switch them on, "
                     "or ignore this if it wasn't you.")


def signup_nudges(db: Session, now_utc: datetime | None = None) -> int:
    """Every few minutes: anyone who reached the say-hi step SIGNUP_NUDGE_MINUTES ago, typed a
    number and ticked the terms, but whose message never came, gets one nudge with a Start
    reminders button (META_SIGNUP_NUDGE_TEMPLATE). Tapping it is their hi. Without an approved
    template nothing is sent: free text to a number that hasn't messaged us wouldn't arrive."""
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    s = get_settings()
    can_template = uses_template(s.meta_signup_nudge_template)
    if not can_template and hasattr(get_provider(), "send_template"):
        return 0
    due = now_utc - timedelta(minutes=SIGNUP_NUDGE_MINUTES)
    stale = now_utc - timedelta(hours=24)  # a day later it's no longer a nudge
    users = db.scalars(select(User).where(
        User.status == UserStatus.pending, User.phone_e164.is_not(None),
        User.consent_at.is_not(None), User.signup_nudged_at.is_(None),
        User.hi_prompted_at.is_not(None), User.hi_prompted_at <= due,
        User.hi_prompted_at >= stale)).all()
    users = [u for u in users if u.bin_settings and u.wa_step is None]
    jobs = [(u.phone_e164, [], SIGNUP_NUDGE_TEXT) for u in users]
    rows = send_template_batch(db, s.meta_signup_nudge_template, jobs, kind="signup_nudge",
                               buttons=[START_PAYLOAD])
    for u in users:
        u.signup_nudged_at = now_utc  # once, whether or not it got through
    db.commit()
    return sum(1 for r in rows if not r.error)


UNRECOGNISED_KEEP_DAYS = 90


def purge_unrecognised(db: Session, now_utc: datetime | None = None) -> int:
    """Delete messages the bot couldn't understand once they're UNRECOGNISED_KEEP_DAYS old
    (they're residents' own words, so we don't keep them longer than we need)."""
    cutoff = (now_utc or datetime.now(ZoneInfo("UTC"))) - timedelta(days=UNRECOGNISED_KEEP_DAYS)
    old = db.scalars(select(ChangeLog).where(ChangeLog.kind == "unrecognised",
                                             ChangeLog.detected_at < cutoff)).all()
    for row in old:
        db.delete(row)
    db.commit()
    return len(old)


def welcome_parts(db: Session, user: User) -> list[str]:
    """The welcome, sent the moment reminders go on as separate short messages: set up, first
    collection, how to stop, and (welcome_share) a nudge to share it with neighbours. Shows the service works and (on WhatsApp) opens the
    24-hour window for free-form replies."""
    first = db.scalar(select(Reminder).where(Reminder.user_id == user.id)
                      .order_by(Reminder.send_at))
    clock = _clock(user.reminder_time)
    # "night_before" is the evening before, at any time the resident picks (4pm is an option).
    # Copy always says "the evening before", never "the day before" or "the night before".
    when = ("the evening before collection" if user.reminder_mode == ReminderMode.night_before
            else "on the morning of collection")
    parts = [f"{get_settings().brand_name} here. You're all set. I'll message you at {clock} {when}."]
    if first:
        names = [b["label"] for b in first.bins]
        bins = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
        parts.append(f"First up: {bins} on {first.collection_date.strftime('%a %-d %b')}.")
    parts.append("Want to bin me off? Reply STOP anytime.")
    nudge = welcome_share(user)
    if nudge:
        parts.append(nudge)
    return [p[:4096] for p in parts]  # WhatsApp's text limit; each is far shorter in practice


def welcome_share(user: User) -> str | None:
    """The fourth welcome message: share Binjamin with neighbours or the street group. Written
    to be forwarded as it is, so the link opens a chat with Binjamin (any message there starts
    sign-up). Only for people who didn't opt out of hearing from us, and only once there's a
    business number to link to. It counts as their one share nudge (share_nudges)."""
    s = get_settings()
    digits = re.sub(r"\D", "", s.meta_display_number)
    if not digits or user.marketing_consent_at is None:
        return None
    return (f"Want to be a certified Binfluencer? Pop me in your street's group chat or send me "
            f"to a friend. All they need to do is say hi to {s.brand_name} on WhatsApp: "
            f"wa.me/{digits}")


def welcome_message(db: Session, user: User) -> str:
    """The whole welcome as one text (what the API and tests return)."""
    return "\n\n".join(welcome_parts(db, user))


def send_welcome(db: Session, phone: str, user: User) -> str:
    """Send the welcome as its separate messages. The first is recorded as kind "welcome" (one
    per sign-up), the rest as "welcome_more". Returns the whole text."""
    import time

    real = get_provider().name == "meta"
    if welcome_share(user):  # the share nudge rides on the welcome: no second one later
        user.share_nudged_at = datetime.now(ZoneInfo("UTC"))
    for i, part in enumerate(welcome_parts(db, user)):
        if i and real:
            time.sleep(0.7)  # WhatsApp doesn't promise order: a beat apart keeps them in sequence
        send_message(db, phone, part, kind="welcome" if i == 0 else "welcome_more")
    return welcome_message(db, user)


def hi_text() -> str:
    """What the say-hi button puts in their WhatsApp: no code to puzzle over. We know who they
    are from the number they message from (the one they typed on the site)."""
    return f"Hey {get_settings().brand_name}, I'M BIN"


def start_link(code: str | None = None) -> str:
    """wa.me link that opens WhatsApp with hi_text() ready to send. (`code` is ignored: links from
    before the change carried one, and those messages still work.)"""
    digits = re.sub(r"\D", "", get_settings().meta_display_number)
    return f"https://wa.me/{digits}?text={quote(hi_text())}"


HI_RE = re.compile(r"\bI\s*['’]?\s*M\s+BIN\b", re.IGNORECASE)  # "I'M BIN", "im bin", "I’m Bin"
START_PAYLOAD = "START_REMINDERS"  # the Start reminders button on the sign-up nudge template


def is_hi(text: str) -> bool:
    return bool(HI_RE.search(text or "")) or (text or "").strip() == START_PAYLOAD


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
    credit_referral(db, user)
    return send_welcome(db, phone, user), "welcome"


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
    A bare hour follows the reminder mode: 7 is 7pm the evening before, 7am on the morning.
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
    return ("the evening before" if user.reminder_mode == ReminderMode.night_before
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
DAY_BEFORE_TIMES = [("16:00", "4pm"), ("17:00", "5pm"), ("18:00", "6pm"), ("19:00", "7pm"), ("20:00", "8pm"), ("21:00", "9pm")]
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
    last = ("menu:resume", "Turn reminders on", "") if paused else ("menu:pause", "Pause reminders", "")
    rows = [("menu:time", "Reminder time", "When I message you the evening before"),
            ("menu:bins", "My bins", "Turn bins on or off"),
            ("menu:wrong", "Wrong collection day", "Tell us your days don't match"),
            last,
            ("menu:delete", "Delete my data", "Remove your account for good")]
    return Reply(text, list_button="Settings", sections=[("Settings", rows)])


def time_menu(user: User) -> Reply:
    def rows(times, prefix, mode):
        return [(f"{prefix}:{hhmm}", title,
                 "Current" if user.reminder_mode == mode and user.reminder_time == hhmm else "")
                for hhmm, title in times]
    # Evening before only: a morning-of reminder lands after many councils' 6-7am pickups. Morning
    # times still work if typed (MORNING 6:30am) or tapped from an old menu.
    return Reply("Pick a time for your reminders.", list_button="Pick a time", sections=[
        ("Evening before", rows(DAY_BEFORE_TIMES, "time", ReminderMode.night_before))])


def _daily_bins(db: Session, user: User) -> list:
    from app.services.signup import bins_out

    return [b for b in bins_out(db, user) if b.daily]


def bins_menu(db: Session, user: User) -> Reply:
    bins = _user_bins(db, user)
    if not bins:
        return Reply("You don't have any bins set up yet.", buttons=[SETTINGS_BUTTON])
    daily = {b.label for b in _daily_bins(db, user)}
    rows = [(f"bin:{b.id}", b.label[:24],
             "Collected daily, no reminders" if b.label in daily and b.enabled
             else "On, tap to turn off" if b.enabled else "Off, tap to turn on")
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


# ---- questions about their collections: "when's my next collection?", "is it recycling this week?"

# A question about timing: when / next / which bin / tomorrow / this week (plus a bin word below).
# "Do you do bulky waste?" or "can you remind me about glass?" aren't: they go to the team.
_ASK_RE = re.compile(r"\b(when|whens|when's|what day|which day|next|which bins?|what bins?|"
                     r"tomorrow|today|tonight|this week|bin day)\b", re.IGNORECASE)
_TOPIC_RE = re.compile(r"\b(collections?|collected|collecting|bins?|bin day|pick ?ups?|picked up|"
                       r"rubbish|recycling|waste|garbage|trash|refuse|garden|food|caddy|glass|"
                       r"bottles|paper|cardboard|landfill|general|going out|goes out|go out|put out)\b",
                       re.IGNORECASE)
# Requests and complaints, not questions: settings changes, missed or wrong collections, and
# holiday questions we can't answer from the calendar alone (those go to the team inbox)
_NOT_A_QUESTION_RE = re.compile(r"\b(turn|switch|stop|pause|delete|change|remind me at|wrong|missed|"
                                r"didn'?t|wasn'?t|weren'?t|not been|never|forgot|complain\w*|"
                                r"christmas|xmas|easter|bank holidays?|new year|boxing day)\b",
                                re.IGNORECASE)
# Words people use for each kind of bin (the category, or words in the bin's own label)
_BIN_WORDS = {
    "recycling": r"recycl\w*|blue",
    "general": r"rubbish|general|landfill|household|refuse|black|residual|trash|garbage",
    "food": r"food|caddy|kitchen",
    "garden": r"garden|green waste|grass",
    "glass": r"glass|bottles?|jars?",
    "paper": r"paper|cardboard|card",
}


def _upcoming(db: Session, user: User) -> list[tuple[date, list[dict]]]:
    """Their reminded collections from the first one that's still to come (today until 7am)."""
    from app.services.signup import first_showable_day

    first = first_showable_day()
    return [(d, b) for d, b in effective_collections(db, user).items() if d >= first]


def collection_answer(db: Session, user: User, text: str) -> Reply | None:
    """A genuine question about their collections, answered from their own calendar: the next
    collection, what goes out tomorrow or this week, or when a particular bin is next.
    Returns None for anything else."""
    t = text.strip()
    if not (_ASK_RE.search(t) and _TOPIC_RE.search(t)) or _NOT_A_QUESTION_RE.search(t):
        return None
    days = _upcoming(db, user)
    tail = (f" I'll remind you at {_clock(user.reminder_time)} {_when(user)}."
            if user.status == UserStatus.active else "")
    if not days:
        return Reply("I don't have any collections coming up for you yet. Tap Settings to check "
                     "your bins.", buttons=[SETTINGS_BUTTON])

    def fmt(d: date) -> str:
        return d.strftime("%a %-d %b")

    low = t.lower()
    for cat, rx in _BIN_WORDS.items():  # a particular bin
        if not re.search(rf"\b({rx})\b", low):
            continue
        for d, bins in days:
            hit = [b["label"] for b in bins
                   if b["category"] == cat or re.search(rf"\b({rx})\b", b["label"].lower())]
            if hit:
                return Reply(f"{_join(hit)}: next out on {fmt(d)}.{tail}")
        return Reply("I don't have that bin on your calendar. Tap Settings, then My bins, to see "
                     "the ones I remind you about.", buttons=[SETTINGS_BUTTON])
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    if re.search(r"\btomorrow\b", low):
        on = dict(days).get(today + timedelta(days=1))
        if on:
            return Reply(f"Tomorrow ({fmt(today + timedelta(days=1))}): "
                         f"{_join([b['label'] for b in on])}.{tail}")
        d, bins = days[0]
        return Reply(f"Nothing tomorrow. Next up: {_join([b['label'] for b in bins])} on {fmt(d)}.")
    if re.search(r"\bthis week\b", low):
        week = [(d, b) for d, b in days if d <= today + timedelta(days=6 - today.weekday())]
        if week:
            return Reply("This week: " + "; ".join(
                f"{_join([b['label'] for b in bins])} on {fmt(d)}" for d, bins in week) + "." + tail)
        d, bins = days[0]
        return Reply(f"Nothing more this week. Next up: {_join([b['label'] for b in bins])} on "
                     f"{fmt(d)}.")
    d, bins = days[0]
    return Reply(f"Next up: {_join([b['label'] for b in bins])} on {fmt(d)}.{tail}")


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
            example = "MORNING 6:30am" if mode == ReminderMode.morning_of else "EVENING 7pm"
            return Reply(f"I didn't catch that time. Try {example}.")
        return _set_time(db, user, mode, hhmm)
    return collection_answer(db, user, t) or _bin_phrase(db, user, t)


# When Binjamin doesn't understand: these take turns (per person), then a pointer to Settings.
NOT_UNDERSTOOD = ("I'm afraid that's beyond my bin grade. I'm just here for the rubbish!",
                  "I'm just a humble bin butler. Rubbish is my speciality!")
FALLBACK_TAIL = "Tap Settings to change your bins or reminder time."
FALLBACK = f"{NOT_UNDERSTOOD[0]} {FALLBACK_TAIL}"


def fallback_text(db: Session, user: User) -> str:
    """The not-understood reply, alternating between the two lines for each person."""
    n = db.scalar(select(func.count()).select_from(ChangeLog).where(
        ChangeLog.scope == "user", ChangeLog.scope_id == user.id,
        ChangeLog.kind == "unrecognised")) or 1
    return f"{NOT_UNDERSTOOD[(n - 1) % len(NOT_UNDERSTOOD)]} {FALLBACK_TAIL}"


DELETE_CONFIRM = ("This deletes your account for good: your number, address, bins and reminders. "
                  "You'll stop getting reminders straight away. Sure?")
DELETED = ("Done. Your account and everything we held about you is deleted, and you won't hear "
           "from us again. You can sign up again any time.")


def _erase(db: Session, user: User, phone: str, text: str) -> str | None:
    """DELETE (or Settings → Delete my data) asks first; DELETE YES or the button deletes the
    account there and then (right to erasure). Returns the reply text, or None."""
    t = text.strip()
    words = t.upper().split()
    if t == "menu:delete" or words in (["DELETE"], ["DELETE", "MY", "DATA"], ["DELETE", "ME"]):
        send_reply(db, phone, DELETE_CONFIRM, kind="reply",
                   buttons=[("menu:delete_yes", "Yes, delete it"), ("menu:settings", "No, keep it")])
        db.commit()
        return DELETE_CONFIRM
    if t == "menu:delete_yes" or words == ["DELETE", "YES"]:
        from app.services.retention import delete_account

        send_reply(db, phone, DELETED, kind="reply")
        delete_account(db, user)  # also removes the messages we sent, this one included
        db.commit()
        return DELETED
    return None


def handle_inbound(db: Session, phone: str, text: str) -> str:
    """Replies to a user's message or tap: the sign-up hi ("start my reminders <code>"), DONE /
    SNOOZE / WRONG / STOP / START, the settings menus (taps, or "settings"), and typed shortcuts
    (TIME 4pm, EVENING 7pm, MORNING, "turn off garden"). Returns the reply text."""
    hi = _handle_hi(db, phone, text)
    if hi:
        if hi[1] != "welcome":  # the welcome went out already, as its separate messages
            send_reply(db, phone, hi[0], kind=hi[1])
        u = db.scalar(select(User).where(User.phone_e164 == phone))
        if u:
            u.last_active_at = datetime.now(ZoneInfo("UTC"))
        db.commit()
        return hi[0]
    from app.services import inbox

    if inbox.enabled() and inbox.wants_human(text) and ":" not in text.split(" ")[0]:
        # "Talk to a human": straight to the team inbox, from anyone, at any step
        send_reply(db, phone, inbox.HANDED_OVER, kind="reply")
        db.commit()
        inbox.post(db, phone, text, "human")
        return inbox.HANDED_OVER
    user = db.scalar(select(User).where(User.phone_e164 == phone))
    from app.services import wa_signup

    if wa_signup.wants(user):  # not signed up yet: sign up right here in WhatsApp
        return wa_signup.handle(db, phone, text, user) or ""
    if is_hi(text) and user.status == UserStatus.active:  # the sign-up hi, sent twice
        msg = 'Your reminders are already on. Message "settings" to change them.'
        send_reply(db, phone, msg, kind="reply")
        db.commit()
        return msg
    if text.startswith(wa_signup.REACT):  # an emoji reaction from someone signed up: no reply
        return ""
    user.last_active_at = datetime.now(ZoneInfo("UTC"))
    if text.strip() == "menu:wrong":  # "Wrong collection day" in the settings menu
        text = "WRONG"
    erase = _erase(db, user, phone, text)
    if erase:
        return erase
    words = text.strip().upper().split()
    cmd = words[0] if words else ""
    last = db.scalar(
        select(Reminder).where(Reminder.user_id == user.id, Reminder.status == ReminderStatus.sent)
        .order_by(Reminder.sent_at.desc())
    )
    reply = None
    to_inbox = None  # posted to the team's Slack inbox after replying (services/inbox.py)
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
    elif cmd == "DONE" and len(words) == 1 and db.scalar(
            select(Reminder.id).where(Reminder.user_id == user.id,
                                      Reminder.status == ReminderStatus.done)):
        # Done tapped again on an old reminder (the buttons stay tappable): nothing left to do
        reply = Reply("Already sorted \U0001F44D I'll be in touch before the next one.")
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
        to_inbox = "wrong_day"
    else:
        # Kept so we can see what people ask for that we don't handle yet. Deleted after
        # UNRECOGNISED_KEEP_DAYS by the worker (purge_unrecognised, daily).
        db.add(ChangeLog(scope="user", scope_id=user.id, kind="unrecognised",
                         diff={"text": text.strip()[:1000]}))
        if inbox.in_conversation(db, phone):
            # The team is talking to them: this goes to the team, not the usual menu
            db.commit()
            inbox.post(db, phone, text, "unrecognised")
            return ""
        db.flush()
        reply = Reply(fallback_text(db, user), buttons=[SETTINGS_BUTTON])
        to_inbox = "unrecognised"
    if cmd in ("DONE", "SNOOZE") and last and len(words) == 1:
        # The reminder's template couldn't carry the referral news: it rides on this reply.
        news = referral_news(db, user)
        if news:
            reply.text = f"{reply.text}\n\n{news[0]}"
            _mark_announced(news[1], datetime.now(ZoneInfo("UTC")))
    send_reply(db, phone, reply.text, kind="reply", buttons=reply.buttons,
               list_button=reply.list_button, sections=reply.sections)
    if cmd == "DONE" and last and len(words) == 1:
        share_after_done(db, user, phone)
    db.commit()
    if to_inbox:
        inbox.post(db, phone, text, to_inbox)
    return reply.text
