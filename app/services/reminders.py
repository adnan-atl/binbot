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
    BinType,
    Referral,
    ChangeLog,
    CollectionEvent,
    Outbox,
    Reminder,
    ReminderMode,
    ReminderStatus,
    User,
    UserBinSetting,
    UserStatus,
)
from app.services import analytics
from app.services.normalize import bin_tag, describe_rule, is_daily
from app.providers.messaging import get_provider, send_contact, send_message, send_reply, send_template_batch, uses_template

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
    enabled_cats = {s.category.value for s in settings if s.enabled}
    for s in settings:
        if not s.enabled:
            continue
        colour, colours = s.colour, []
        if not colour and s.bin_type_id and not s.override_rule:
            from app.services.schedule import known_colour, recycling_containers

            bt = db.get(BinType, s.bin_type_id)
            rk = user.property.round_key if user.property else None
            colour = known_colour(db, bt, rk) if bt else None
            parts = recycling_containers(db, bt, rk, enabled_cats) if bt else []
            if parts:  # several recycling containers: name them all, not one colour
                colour, colours = None, [c for c, _ in parts]
        bin_info = {"label": s.label, "category": s.category.value, "setting_id": s.id,
                    "colour": colour, "colours": colours}
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
    retrying = set()
    for r in db.scalars(
        select(Reminder).where(Reminder.user_id == user.id,
                               Reminder.status == ReminderStatus.scheduled)
    ):
        if r.attempts and user.status == UserStatus.active:
            retrying.add(r.collection_date)  # mid-retry: keep it, or it'd be dropped as past
            continue
        db.delete(r)
    db.flush()
    if user.status != UserStatus.active or not user.phone_e164:
        return 0
    n = 0
    now_ = datetime.now(ZoneInfo(user.timezone or "Europe/London"))
    for d, bins in effective_collections(db, user).items():
        send_at = send_at_for(user, d)
        if send_at < now_ or d in retrying:
            continue
        db.add(Reminder(user_id=user.id, collection_date=d, send_at=send_at,
                        bins=[{"label": b["label"], "category": b["category"], "colour": b.get("colour"),
                               "colours": b.get("colours") or []}
                              for b in bins]))
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
    analytics.track(ref_id, "referral_credited")


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
    names = [bin_tag(b) for b in r.bins]
    when = "tomorrow" if user.reminder_mode == ReminderMode.night_before else "today"
    if len(names) == 1:
        head = f"{names[0]} goes out {when}"
    else:
        head = f"{', '.join(names[:-1])} and {names[-1]} go out {when}"
    day = r.collection_date.strftime("%a %-d %b")
    return (f"{head} ({day}).\nReply DONE when it's out, SNOOZE for a nudge later, or WRONG if "
            f"that's not your bin day. STOP to stop.")


last_errors = 0  # failed sends in the latest dispatch_due run, retries included (the heartbeat)


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
        params = [_join([bin_tag(b) for b in r.bins]), when, r.collection_date.strftime("%a %-d %b")]
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
    global last_errors
    sent, gave_up = 0, []
    last_errors = sum(1 for row in rows if row.error)
    for (r, _, news_rows), row in zip(plain + with_news, rows):
        if row.error:
            r.attempts = (r.attempts or 0) + 1
            retry_at = _retry_at(db.get(User, r.user_id), r, now_utc)
            if retry_at:  # try again shortly: a blip at Meta, a template mid-review
                r.send_at = retry_at
            else:
                r.status = ReminderStatus.failed
                gave_up.append((r, row.error))
                analytics.track(r.user_id, "reminder_failed")
        else:
            r.status = ReminderStatus.sent
            r.sent_at = now_utc
            r.provider_message_id = row.provider_message_id
            db.get(User, r.user_id).last_active_at = now_utc  # a reminder that reaches them is use
            if news_rows:
                _mark_announced(news_rows, now_utc)
            sent += 1
            u = db.get(User, r.user_id)
            analytics.track(u, "reminder_sent", bins=len(r.bins or []), mode=u.reminder_mode.value,
                            time=u.reminder_time, retried=bool(r.attempts),
                            with_news=bool(news_rows))
    db.commit()
    if gave_up:
        from app.services import inbox

        inbox.alert_failed_reminders(db, gave_up)
    return sent


# A failed reminder is tried again this many minutes after each failure, then given up on.
RETRY_AFTER_MINUTES = (10, 30)
# ...but never once it's too late to be useful: the collection day's morning.
RETRY_UNTIL = time(8, 0)


def _retry_at(user: User, r: Reminder, now_utc: datetime) -> datetime | None:
    """When to try a failed reminder again, or None to give up (out of tries, or too late)."""
    if r.attempts > len(RETRY_AFTER_MINUTES):
        return None
    if now_utc.tzinfo is None:  # SQLite hands back naive UTC
        now_utc = now_utc.replace(tzinfo=ZoneInfo("UTC"))
    at = now_utc + timedelta(minutes=RETRY_AFTER_MINUTES[r.attempts - 1])
    tz = ZoneInfo(user.timezone or "Europe/London")
    if at > datetime.combine(r.collection_date, RETRY_UNTIL, tz):
        return None
    return at


# ---- "share with neighbours": once, after they've had their first reminder --------------------

def share_link(user: User) -> str:
    """binjamin.uk/r/<code>: counts the tap and opens the website with the referral saved
    (app/main.py). Every share, WhatsApp or website, sends people here: the site shows who
    Binjamin is, and sign-ups through it are credited to whoever shared."""
    return f"{get_settings().public_base_url.rstrip('/')}/r/{user.referral_code}"


def share_message(user: User) -> str:
    """The message people forward to neighbours or their street's group chat. Sent on its own,
    so forwarding it carries nothing else; the link shows a preview with Binjamin's picture.
    Same words as the website's share screen."""
    s = get_settings()
    return (f"\U0001F3A9 Thought you might like this little gem!\n\n{s.brand_name} is a free WhatsApp "
            f"assistant that reminds you which bin to put out the night before collection. Keeps "
            f"track of your council's schedule, so you don't have to.\n\nTakes a minute to set up: "
            f"{share_link(user)}")


SHARE_INTRO = ("Help your community and become a certified Binfluencer. Copy & Paste my next "
               "message to your street's group chat or a friend.")


def share_text(user: User) -> str:
    """The share nudge as one text (the template's fallback body)."""
    return f"If you want to share {get_settings().brand_name} with neighbours or friends, send them this link: {share_link(user)}"


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
    send_share(db, phone, user, kind="share")
    user.share_nudged_at = datetime.now(ZoneInfo("UTC"))
    return True


def send_share(db: Session, phone: str, user: User, kind: str, intro: str = SHARE_INTRO) -> None:
    """The intro, then the message to forward as its own bubble."""
    import time

    send_message(db, phone, intro, kind=kind)
    if get_provider().name == "meta":
        time.sleep(0.7)  # keep them in order
    send_message(db, phone, share_message(user), kind=kind)
    analytics.track(user, "share_message_sent", trigger=kind)


def share_nudges(db: Session, now_utc: datetime | None = None) -> int:
    """Sunday 7:30pm: the share nudge for everyone whose first reminded collection has been and
    gone and who hasn't had it yet (didn't tap Done), so it lands the first Sunday evening after
    Binjamin has done its job, when people are messaging and thinking about the week ahead. Goes as the
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
    # Readable copy of the bin_share_sunday template (docs/whatsapp-templates.md, section 4)
    jobs = [(u.phone_e164, [s.brand_name, share_link(u)],
             f"Hope {s.brand_name} made bin day a bit easier this week \U0001F3A9 Know a neighbour "
             f"who always forgets theirs? Pop this link in your street's group chat or send it to "
             f"a friend: {share_link(u)} \U0001F517") for u in users]
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
    """Every few minutes: anyone who reached the last step SIGNUP_NUDGE_MINUTES ago, typed a
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
        analytics.track(u, "signup_nudge_sent")
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
    """The welcome, sent the moment reminders go on as separate short messages: we found their
    home and got the calendar from their council (the reassurance), the first collection and
    when they'll hear from us, and (welcome_share) a nudge to share it with neighbours. The
    only playful messages: reminders themselves stay plain. Opens the 24-hour window too."""
    first = db.scalar(select(Reminder).where(Reminder.user_id == user.id)
                      .order_by(Reminder.send_at))
    clock = _clock(user.reminder_time)
    # "night_before" is the evening before, at any time the resident picks (4pm is an option).
    # Bot messages say "the evening before", never "the day before" or "the night before". (The
    # share message, which is marketing copy, says "the night before": share_message.)
    when = ("the evening before" if user.reminder_mode == ReminderMode.night_before
            else "on the morning")
    brand = get_settings().brand_name
    home, council = home_line(user), council_name(user)
    hello = f"{brand} here! Found you at {home}. Lovely spot." if home else f"{brand} here!"
    if council:
        hello += (f"\n\nI've had a word with my friends at {council} and got your bin rounds "
                  f"all worked out.")
    parts = [hello]
    if first:
        names = [bin_tag(b) for b in first.bins]
        bins = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
        parts.append(f"Next up: {bins} on {first.collection_date.strftime('%a %-d %b')}. "
                     f"I'll give you a nudge at {clock} {when}.")
    else:
        parts.append(f"I'll give you a nudge at {clock} {when} each collection.")
    nudge = welcome_share(user)
    if nudge:
        parts.append(nudge)
    return [p[:4096] for p in parts]  # WhatsApp's text limit; each is far shorter in practice


def home_line(user: User) -> str:
    """Their home as people say it: '12 Church Street, Malmesbury' (number and street, then the
    town), from the address the council lookup gave us. Empty if we don't have one."""
    prop = user.property
    if prop is None or not prop.address_line:
        return ""
    pc = (prop.postcode or "").replace(" ", "").upper()
    parts = [p.strip() for p in prop.address_line.split(",") if p.strip()
             and p.replace(" ", "").upper() != pc]
    if not parts:
        return ""
    # "46, SILVERDALE ROAD, ..." -> "46 Silverdale Road"; "FLAT 2, 10 ELM ROAD" stays as written
    merged = []
    for part in parts:  # a bare house number joins the street after it
        if merged and re.fullmatch(r"\d+[A-Za-z]?", merged[-1].split(", ")[-1]) and not re.match(r"\d", part):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    parts = merged
    if len(parts) > 2 and re.match(r"(flat|apartment|unit|maisonette)\b", parts[0], re.I):
        parts = [f"{parts[0]}, {parts[1]}"] + parts[2:]  # "Flat 2, 10 High Street"
    street, town = parts[0], (parts[-1] if len(parts) > 1 else "")
    fix = lambda t: re.sub(r"\b[A-Z][A-Z']+\b", lambda m: m.group(0).capitalize(), t)  # SHOUTY -> Shouty
    if town == street:
        town = ""
    return ", ".join(fix(x) for x in (street, town) if x)


def council_name(user: User) -> str:
    """'Wiltshire Council', 'Hammersmith and Fulham Council'. Names that already say council
    (or city) stay as they are."""
    c = user.property.council if user.property else None
    if c is None or not c.name:
        return ""
    return c.name if re.search(r"\b(council|corporation)\b", c.name, re.I) else f"{c.name} Council"


def display_number() -> str:
    """The business number written for people: +44 7849 468697."""
    d = re.sub(r"\D", "", get_settings().meta_display_number)
    return f"+{d[:2]} {d[2:6]} {d[6:]}" if d.startswith("44") and len(d) == 12 else f"+{d}"


def invite_text(code: str) -> str:
    """What a personal WhatsApp invite pre-fills: the reference lets Binjamin credit whoever
    shared it when their friend finishes signing up (wa_signup.REF_RE reads it)."""
    return f"Hey {get_settings().brand_name} \U0001F44B (invited by a neighbour, ref {code})"


def invite_wa_link(code: str) -> str:
    """wa.me link with invite_text() ready to send (only old invites still use it)."""
    digits = re.sub(r"\D", "", get_settings().meta_display_number)
    return f"https://wa.me/{digits}?text={quote(invite_text(code))}"


def _already_invited(user: User, code: str) -> str:
    """Reply when someone already signed up sends an invite message (an old /w/ link)."""
    return ("You're already signed up, so no need for an invite \U0001F44D To share me with friends "
            f"and neighbours, send them your link: {share_link(user)}")




def local_number() -> str:
    """The business number as people write it in the UK: 07849 468697."""
    d = re.sub(r"\D", "", get_settings().meta_display_number)
    return f"0{d[2:6]} {d[6:]}" if d.startswith("44") and len(d) == 12 else display_number()


def welcome_share(user: User) -> str | None:
    """The fourth welcome message: asks them to forward the next one (share_message, with their
    tracked link) to neighbours or the street group. Only for people who didn't opt out of
    hearing from us, and only once there's a business number. It counts as their one share
    nudge (share_nudges). Off unless SHARE_IN_WELCOME: asking before Binjamin has reminded them
    of anything was too soon (they share after their first Done, or on Sunday evening)."""
    s = get_settings()
    if not s.share_in_welcome or not re.sub(r"\D", "", s.meta_display_number) \
            or user.marketing_consent_at is None:
        return None
    return SHARE_INTRO


def binjamin_card() -> dict:
    """Binjamin's contact card, sent after the share nudge for people to forward. With wa_id set,
    WhatsApp shows it as the business account, with a Message button."""
    s = get_settings()
    d = re.sub(r"\D", "", s.meta_display_number)
    return {"name": {"formatted_name": s.brand_name, "first_name": s.brand_name},
            "org": {"company": f"{s.brand_name} by {s.parent_name}"},
            "phones": [{"phone": display_number(), "type": "WORK", "wa_id": d}],
            "urls": [{"url": s.public_base_url.rstrip("/"), "type": "WORK"}]}


def tomorrow_note(db: Session, user: User, now: datetime | None = None) -> str | None:
    """Signing up after a reminder's time: that reminder won't come, so the welcome ends with it
    instead ("And don't forget your … tomorrow!", or "… today, out by 7am!" before 7am on a
    collection day)."""
    from app.services.signup import SAME_DAY_CUTOFF

    tz = ZoneInfo(user.timezone or "Europe/London")
    now = (now or datetime.now(tz)).astimezone(tz)
    cal = effective_collections(db, user)
    # Signing up in the small hours of a collection day: its reminder went last evening, and
    # the bins still need to go out this morning.
    today = cal.get(now.date()) if now.time() < SAME_DAY_CUTOFF else None
    if today:
        return f"And don't forget your {_join([bin_tag(b) for b in today])} today, out by 7am!"
    tomorrow = now.date() + timedelta(days=1)
    bins = cal.get(tomorrow)
    if not bins or send_at_for(user, tomorrow) > now:
        return None
    return f"And don't forget your {_join([bin_tag(b) for b in bins])} tomorrow!"


def welcome_message(db: Session, user: User) -> str:
    """The whole welcome as one text (what the API and tests return)."""
    note = tomorrow_note(db, user)
    share = [share_message(user)] if welcome_share(user) else []
    return "\n\n".join(welcome_parts(db, user) + share + ([note] if note else []))


def send_welcome(db: Session, phone: str, user: User, via: str = "website") -> str:
    """Send the welcome as its separate messages. The first is recorded as kind "welcome" (one
    per sign-up), the rest as "welcome_more". Returns the whole text."""
    import time

    real = get_provider().name == "meta"
    if welcome_share(user):  # the share nudge rides on the welcome: no second one later
        user.share_nudged_at = datetime.now(ZoneInfo("UTC"))
    parts = welcome_parts(db, user)
    share = welcome_share(user)
    for i, part in enumerate(parts):
        if i and real:
            time.sleep(0.7)  # WhatsApp doesn't promise order: a beat apart keeps them in sequence
        send_message(db, phone, part, kind="welcome" if i == 0 else "welcome_more")
        if share and part == share:  # then the message to forward, with their tracked link
            if real:
                time.sleep(0.7)
            send_message(db, phone, share_message(user), kind="welcome_share")
    note = tomorrow_note(db, user)
    if note:  # tomorrow's reminder time has already gone: say it now, last
        if real:
            time.sleep(0.7)
        send_message(db, phone, note, kind="welcome_more")
    db.flush()
    from app.services import inbox as _inbox

    _inbox.announce_signup(db, user, via)  # every sign-up gets one welcome: the Slack tracker
    analytics.track(user, "reminders_switched_on", via=via, mode=user.reminder_mode.value,
                    time=user.reminder_time, offers_ok=user.marketing_consent_at is not None,
                    bins=sum(1 for b in user.bin_settings if b.enabled))
    return welcome_message(db, user)


def hi_text() -> str:
    """The message the website's last step puts in their WhatsApp, ready to send: plain words
    saying what it does, no code to puzzle over. We know who they are from the number they
    message from (the one they typed on the site)."""
    return f"Hi {get_settings().brand_name}, please start my bin reminders"


def start_link(code: str | None = None) -> str:
    """wa.me link that opens WhatsApp with hi_text() ready to send. (`code` is ignored: links from
    before the change carried one, and those messages still work.)"""
    digits = re.sub(r"\D", "", get_settings().meta_display_number)
    return f"https://wa.me/{digits}?text={quote(hi_text())}"


# Their sign-up message: today's "please start my bin reminders", or "I'M BIN" from links and QR
# codes made before (still out there in old tabs and screenshots).
HI_RE = re.compile(r"\bstart\s+my\s+bin\s+reminders\b|\bI\s*['’]?\s*M\s+BIN\b", re.IGNORECASE)
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
    share: bool = False  # follow it with the message to forward (share_message), then the card


SETTINGS_BUTTON = ("menu:settings", "Settings")
# The times offered in the menu: (id, title). Typed TIME 5:45pm still works for anything else.
DAY_BEFORE_TIMES = [("16:00", "4pm"), ("17:00", "5pm"), ("18:00", "6pm"), ("19:00", "7pm"), ("20:00", "8pm"), ("21:00", "9pm")]


def _evening_ok(hhmm: str) -> bool:
    """Reminders go the evening before: any time from noon to 11pm the day before."""
    return "12:00" <= hhmm <= "23:00"


def _evening_only(user: User) -> Reply:
    return Reply("I only send reminders the evening before, so there's time to put your bins out. "
                 "Pick a time:", list_button="Pick a time", sections=time_menu(user).sections)


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
        names = [bin_tag(b) for b in r.bins]
        if label is None or label in [b["label"] for b in r.bins]:
            return f" Next: {_join(names)} on {r.collection_date.strftime('%a %-d %b')}."
    return ""


_OFF_NOTE = " Reminders are paused at the moment. Reply START to turn them back on."


# ---- menus -------------------------------------------------------------------------------------

def share_reply() -> Reply:
    """Settings → Share Binjamin (or typed "share"): the message to forward follows, with their
    tracked link, then Binjamin's card for anyone who'd rather forward that."""
    return Reply("Forward my next message to your street's group chat or a friend \U0001F447", share=True)


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
            ("menu:share", f"Share {get_settings().brand_name}"[:24], "Send me to your neighbours"),
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
    analytics.track(user, "reminder_time_changed", mode=mode.value, time=hhmm)
    tail = _next_reminder(db, user) if user.status == UserStatus.active else _OFF_NOTE
    return Reply(f"Done. I'll remind you {_when(user)} at {_clock(hhmm)}.{tail}")


def _set_bin(db: Session, user: User, b: UserBinSetting, on: bool) -> Reply:
    after = [("menu:bins", "My bins"), ("menu:done", "Done")]
    if b.enabled == on:
        return Reply(f"{b.label} is already {'on' if on else 'off'}.", buttons=after)
    b.enabled = on
    db.flush()
    rebuild_reminders(db, user)
    analytics.track(user, "bin_switched_on" if on else "bin_switched_off",
                    category=b.category.value)
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
    analytics.track(user, "reminders_paused")
    return Reply("Reminders are off. Reply START or tap Turn reminders on when you want them back.",
                 buttons=[("menu:resume", "Turn reminders on")])


def _resume(db: Session, user: User) -> Reply:
    user.status = UserStatus.active
    rebuild_reminders(db, user)
    analytics.track(user, "reminders_resumed")
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
        analytics.track(user, "menu_tapped", item=arg)
        return {"settings": lambda: settings_menu(db, user), "time": lambda: time_menu(user),
                "bins": lambda: bins_menu(db, user), "pause": lambda: _pause(db, user),
                "resume": lambda: _resume(db, user),
                "share": lambda: share_reply(),
                "done": lambda: Reply("All set.")}.get(arg, lambda: settings_menu(db, user))()
    if kind == "time_m":  # a morning time tapped on an old menu: evening only now
        return _evening_only(user)
    if kind == "time":
        if arg not in dict(DAY_BEFORE_TIMES):
            return time_menu(user)
        return _set_time(db, user, ReminderMode.night_before, arg)
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
_ASK_RE = re.compile(r"\b(when|whens|when's|what day|which day|what days|which days|next|"
                     r"which bins?|what bins?|tomorrow|today|tonight|this week|bin day|"
                     r"my dates|dates|schedule|calendar|upcoming|coming up)\b",
                     re.IGNORECASE)
# Asking for the list rather than just the next one
_LIST_RE = re.compile(r"\b(dates|schedule|calendar|upcoming|coming up|what days|which days|all)\b",
                      re.IGNORECASE)
_TOPIC_RE = re.compile(r"\b(collections?|collected|collecting|bins?|bin day|pick ?ups?|picked up|"
                       r"rubbish|recycling|waste|garbage|trash|refuse|garden|food|caddy|glass|"
                       r"bottles|paper|cardboard|landfill|general|going out|goes out|go out|put out|"
                       r"dates?|schedule|calendar|days|upcoming|coming up)\b",
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
            hit = [bin_tag(b) for b in bins
                   if b["category"] == cat or re.search(rf"\b({rx})\b", b["label"].lower())]
            if hit:
                return Reply(f"{_join(hit)}: next out on {fmt(d)}.{tail}")
        from app.services.signup import bins_out

        for b in bins_out(db, user):  # on their list, just not reminded about
            if b.category == cat or re.search(rf"\b({rx})\b", b.label.lower()):
                if not b.enabled:
                    return Reply(f"You've turned off reminders for {b.label}. Tap Settings, then "
                                 "My bins, to turn them back on.", buttons=[SETTINGS_BUTTON])
                if b.daily:
                    return Reply(f"{b.label} goes out every day, so I don't send reminders for it.")
        return Reply("I don't have that bin on your calendar. Tap Settings, then My bins, to see "
                     "the ones I remind you about.", buttons=[SETTINGS_BUTTON])
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    if re.search(r"\btomorrow\b", low):
        on = dict(days).get(today + timedelta(days=1))
        if on:
            return Reply(f"Tomorrow ({fmt(today + timedelta(days=1))}): "
                         f"{_join([bin_tag(b) for b in on])}.{tail}")
        d, bins = days[0]
        return Reply(f"Nothing tomorrow. Next up: {_join([bin_tag(b) for b in bins])} on {fmt(d)}.")
    if re.search(r"\bthis week\b", low):
        week = [(d, b) for d, b in days if d <= today + timedelta(days=6 - today.weekday())]
        if week:
            return Reply("This week: " + "; ".join(
                f"{_join([bin_tag(b) for b in bins])} on {fmt(d)}" for d, bins in week) + "." + tail)
        d, bins = days[0]
        return Reply(f"Nothing more this week. Next up: {_join([bin_tag(b) for b in bins])} on "
                     f"{fmt(d)}.")
    if _LIST_RE.search(low):  # "remind me of my dates", "what's my schedule?"
        lines = [f"{fmt(d)}: {_join([bin_tag(b) for b in bins])}" for d, bins in days[:4]]
        return Reply("Your next collections:\n" + "\n".join(lines) + ("\n" + tail.strip() if tail else ""))
    d, bins = days[0]
    return Reply(f"Next up: {_join([bin_tag(b) for b in bins])} on {fmt(d)}.{tail}")


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
    if head in ("SHARE",) and len(words) == 1:
        return share_reply()
    if head in ("SETTINGS", "MENU", "HELP", "CHANGE") and len(words) == 1:
        return settings_menu(db, user)
    if head == "BINS" and len(words) == 1:  # hidden alias
        return bins_menu(db, user)
    if head in ("OFF", "ON") and rest:  # hidden aliases: OFF 2, ON garden
        bins = _user_bins(db, user)
        hits = _find_bin(bins, rest)
        return _set_bin(db, user, hits[0], head == "ON") if len(hits) == 1 else bins_menu(db, user)
    if head == "TIME":
        hhmm = parse_time(rest, ReminderMode.night_before) if rest else None
        if hhmm and not _evening_ok(hhmm):
            return _evening_only(user)
        if not hhmm:
            return Reply("I didn't catch that time. Try TIME 7pm or TIME 18:30, or pick one below.",
                         list_button="Pick a time", sections=time_menu(user).sections)
        return _set_time(db, user, user.reminder_mode, hhmm)
    if head == "MORNING":  # reminders only ever come the evening before
        return _evening_only(user)
    if head in ("EVENING", "NIGHT") or words[:2] == ["DAY", "BEFORE"]:
        arg = " ".join(words[2:]) if words[:2] == ["DAY", "BEFORE"] else rest
        hhmm = parse_time(arg, ReminderMode.night_before) if arg else _DEFAULT_TIME[ReminderMode.night_before]
        if not hhmm or not _evening_ok(hhmm):
            return Reply("I didn't catch that time. Try EVENING 7pm.")
        return _set_time(db, user, ReminderMode.night_before, hhmm)
    return collection_answer(db, user, t) or _bin_phrase(db, user, t)


# When Binjamin doesn't understand: these take turns (per person), then a pointer to Settings.
NOT_UNDERSTOOD = ("I'm afraid that's beyond my bin grade. I'm just here for the rubbish!",
                  "I'm just a humble bin butler. Rubbish is my speciality!")
OPT_OUT_RE = re.compile(r"(please\s+)?(stop|unsubscribe|opt[ -]?out|cancel|end|quit|pause)"
                        r"(\s+(please|it|now|all|everything|messaging( me)?|texting( me)?|"
                        r"sending( me)?( messages)?|reminders|the reminders|my reminders))*"
                        r"(\s+please)?[\s.!]*", re.IGNORECASE)
FALLBACK_TAIL = "Tap Settings to change your bins or reminder time."
# Acknowledgements that need no reply at all (and aren't worth the team's attention).
ACK_RE = re.compile(r"(ok(ay|ey|ie|i)?|o\.k\.?|k+|alright|all right|cool|got it|noted|sure|fine|"
                    r"will do|roger|sounds good|no worries|np|yep|yup|kk)", re.IGNORECASE)


def is_ack(text: str) -> bool:
    """'Ok', 'ok!', 'Got it 👍', or just emoji: an acknowledgement, not a question."""
    t = (text or "").strip()
    if t and not re.search(r"[A-Za-z0-9]", t):
        return True  # only emoji / punctuation
    words = re.sub(r"[^\w\s.']", " ", t).strip(" .!")
    return bool(ACK_RE.fullmatch(re.sub(r"\s+", " ", words)))
FALLBACK = f"{NOT_UNDERSTOOD[0]} {FALLBACK_TAIL}"
# The not-understood reply isn't sent straight away: people often type in bursts, and a reply to
# each half-sentence is spam. Binjamin waits until they've been quiet for FALLBACK_WAIT, answers
# the whole burst once (send_fallbacks, run by the worker), and at most once per FALLBACK_EVERY.
# Everything they send still goes to the team's inbox, so a person can pick it up.
FALLBACK_WAIT = timedelta(seconds=60)
FALLBACK_EVERY = timedelta(hours=24)
FALLBACK_STALE = timedelta(minutes=30)  # the worker was down: too late to answer, leave it


# Things people ask that Binjamin can't do, each with a reply that says so and points them the
# right way. (pattern, reply, tell the team?) The first match wins; {council} is their council.
_TOPICS = [
    (r"\b(missed|wasn'?t (collected|emptied|picked up)|weren'?t (collected|emptied)|"
     r"not (been )?(collected|emptied)|didn'?t (collect|empty|come)|never came|left my bin)\b",
     "Sorry about that. I can't report missed collections, but {council} can: search "
     "\"{council} missed bin collection\". I'll keep reminding you as normal.", True),
    (r"\b(bulky|sofa|couch|mattress|fridge|freezer|furniture|wardrobe|washing machine|"
     r"old (bed|table|chairs?)|large items?)\b",
     "Bulky items are collected by {council}, usually for a fee. I can't book that for you, but "
     "search \"{council} bulky waste\" to arrange it.", True),
    (r"\b(new bin|replacement bin|broken bin|stolen|lost (my |the )?bin|damaged bin|bin (is )?"
     r"(broken|cracked|missing)|extra bin|another bin|bigger bin)\b",
     "New and replacement bins come from {council}. Search \"{council} new bin\" to request one.",
     True),
    (r"\b(christmas|xmas|easter|bank holidays?|new year|boxing day)\b",
     "When {council} moves collections for a bank holiday, I'll message you the new day.", True),
    (r"\b(tip|dump|recycling cent(re|er)|household waste (site|cent(re|er)))\b",
     "Tips and recycling centres are run by {council}. Search \"{council} recycling centre\" for "
     "opening times.", True),
    (r"\b(can i (recycle|put)|what (goes|can go) in|which bin (does|do|for)|where does .+ go|"
     r"is .+ recyclable|recycle (this|it)|goes in which bin)\b",
     "I don't know {council}'s rules for every item, sorry. Search \"{council} what goes in my "
     "bin\" for the full list.", True),
    (r"\b(weather|forecast|rain|raining|sunny|temperature)\b",
     "I'm a bin butler, not a weatherman! I can only help with your bins.", False),
    (r"\b(council tax|energy|electricity|gas bill|broadband|insurance|mortgage|water bill|bills?)\b",
     "That's beyond my bin grade, sorry. I can only help with your bin collections.", True),
    (r"^(no thanks|no thank you|no ta)\b", "No problem!", False),
    (r"^(?!no\b).*\b(thanks|thank you|thank u|cheers|ta|nice one|great|perfect|brilliant|lovely)\b",
     "You're welcome! \U0001F44B", False),
    (r"^(hi|hello|hey|hiya|morning|evening|afternoon|yo)\b[\s!.]*$",
     "Hi! I'll message you the evening before your next collection. Ask me \"when's my next "
     "collection?\" any time.", False),
    (r"\b(who are you|what are you|what is this|what's this|who is this|are you (a )?(bot|real|human))\b",
     "I'm {brand}, by {parent}. I message you the evening before bin day with which bins to "
     "put out.", False),
]


def topic_reply(user: User, text: str) -> tuple[str, bool] | None:
    """A tailored "can't help with that, but…" for things people commonly ask, or None.
    Returns (reply, whether the team inbox should see it)."""
    s = get_settings()
    council = (user.property.council.name if user.property and user.property.council else
               "your council")
    low = text.strip().lower()
    for rx, reply, tell in _TOPICS:
        if re.search(rx, low):
            return reply.format(council=council, brand=s.brand_name, parent=s.parent_name), tell
    return None


def fallback_text(db: Session, user: User) -> str:
    """The not-understood reply, alternating between the two lines for each person."""
    n = db.scalar(select(func.count()).select_from(ChangeLog).where(
        ChangeLog.scope == "user", ChangeLog.scope_id == user.id,
        ChangeLog.kind == "fallback_sent")) or 0
    return f"{NOT_UNDERSTOOD[n % len(NOT_UNDERSTOOD)]} {FALLBACK_TAIL}"


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=ZoneInfo("UTC"))


def send_fallbacks(db: Session, now_utc: datetime | None = None) -> int:
    """Answer bursts of messages Binjamin didn't understand, once each, after the person has been
    quiet for FALLBACK_WAIT. Stays quiet when something they sent since got a proper answer, when
    the team is talking to them, or when they already had a fallback in the last FALLBACK_EVERY.
    Returns how many fallbacks went out."""
    from app.services import inbox

    now = now_utc or datetime.now(ZoneInfo("UTC"))
    rows = db.scalars(select(ChangeLog).where(
        ChangeLog.scope == "user", ChangeLog.kind == "unrecognised",
        ChangeLog.detected_at >= now - FALLBACK_STALE)).all()
    pending: dict[str, list[ChangeLog]] = {}
    for r in rows:
        if (r.diff or {}).get("pending"):
            pending.setdefault(r.scope_id, []).append(r)
    sent = 0
    for user_id, burst in pending.items():
        first = min(_aware(r.detected_at) for r in burst)
        if now - max(_aware(r.detected_at) for r in burst) < FALLBACK_WAIT:
            continue  # still typing
        for r in burst:
            r.diff = {k: v for k, v in r.diff.items() if k != "pending"}
        user = db.get(User, user_id)
        if user is None or not user.phone_e164:
            continue
        phone = user.phone_e164
        answered = db.scalar(select(Outbox.id).where(
            Outbox.to_phone == phone, Outbox.kind == "reply", Outbox.created_at >= first))
        recent = db.scalar(select(ChangeLog.id).where(
            ChangeLog.scope == "user", ChangeLog.scope_id == user_id,
            ChangeLog.kind == "fallback_sent", ChangeLog.detected_at >= now - FALLBACK_EVERY))
        if answered or recent or inbox.in_conversation(db, phone):
            continue
        send_reply(db, phone, fallback_text(db, user), kind="reply", buttons=[SETTINGS_BUTTON])
        db.add(ChangeLog(scope="user", scope_id=user_id, kind="fallback_sent",
                         diff={"messages": len(burst)}, detected_at=now))
        sent += 1
    db.commit()
    return sent


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
        analytics.track(user, "account_deleted", via="whatsapp")
        delete_account(db, user)  # also removes the messages we sent, this one included
        db.commit()
        return DELETED
    return None


def handle_inbound(db: Session, phone: str, text: str) -> str:
    """Replies to a user's message or tap: the sign-up hi ("start my reminders <code>"), DONE /
    SNOOZE / WRONG / STOP / START, the settings menus (taps, or "settings"), and typed shortcuts
    (TIME 4pm, EVENING 7pm, "turn off garden"). Returns the reply text."""
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
        analytics.track(db.scalar(select(User.id).where(User.phone_e164 == phone)),
                        "asked_for_human")
        return inbox.HANDED_OVER
    user = db.scalar(select(User).where(User.phone_e164 == phone))
    from app.services import wa_signup

    if wa_signup.wants(user):  # not signed up yet: sign up right here in WhatsApp
        return wa_signup.handle(db, phone, text, user) or ""
    if wa_signup.REF_RE.search(text) and "invited by a neighbour" in text.lower():
        # Someone already signed up tapped an invite (their own, or a friend's)
        msg = _already_invited(user, wa_signup.REF_RE.search(text).group(1))
        send_reply(db, phone, msg, kind="reply")
        db.commit()
        return msg
    if text.startswith(wa_signup.P):
        # A button from the sign-up messages, tapped after they'd already signed up: nothing to
        # do, and it's not a message for the team
        msg = 'You\'re already signed up \U0001F44D Message "settings" to change anything.'
        send_reply(db, phone, msg, kind="reply")
        db.commit()
        return msg
    if re.search(r"/r/[A-Za-z0-9_-]{4,12}\b", text) and len(text.split()) <= 3:
        # They sent us a share link (probably their own, to try it): not one for the team
        msg = ("That's a share link \U0001F44D Send it to friends and neighbours, or pop it in your "
               "street's group chat, and they can sign up from it.")
        send_reply(db, phone, msg, kind="reply")
        db.commit()
        return msg
    if is_hi(text) and user.status == UserStatus.stopped:  # the sign-up hi from a paused account
        reply = _resume(db, user)
        send_reply(db, phone, reply.text, kind="reply")
        db.commit()
        return reply.text
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
    if OPT_OUT_RE.fullmatch(text.strip()):
        text = "STOP"  # every way of saying stop works, as WhatsApp expects
    words = text.strip().upper().split()
    cmd = words[0] if words else ""
    last = db.scalar(
        select(Reminder).where(Reminder.user_id == user.id, Reminder.status == ReminderStatus.sent)
        .order_by(Reminder.sent_at.desc())
    )
    # The latest reminder that reached them, whatever they did with it (Done / Snooze / Wrong on
    # an older message still mean that collection)
    latest = last or db.scalar(
        select(Reminder).where(Reminder.user_id == user.id, Reminder.sent_at.is_not(None))
        .order_by(Reminder.sent_at.desc()))
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
    elif cmd == "DONE" and latest is not None and latest.status == ReminderStatus.snoozed:
        # Done after Snooze, before the nudge came: done, and the nudge is called off
        latest.status, latest.reply = ReminderStatus.done, "done"
        for r in db.scalars(select(Reminder).where(
                Reminder.user_id == user.id, Reminder.collection_date == latest.collection_date,
                Reminder.status == ReminderStatus.scheduled)):
            r.status = ReminderStatus.cancelled
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
    elif cmd == "SNOOZE" and len(words) == 1 and latest is not None \
            and latest.status == ReminderStatus.snoozed:
        reply = Reply("Already snoozed: I'll nudge you again soon.")
    elif cmd == "WRONG" and latest is not None:
        last = latest
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
    elif is_ack(text):
        # "Ok", "👍", "got it": nothing to answer, nothing for the team. A reply would only annoy.
        db.commit()
        return ""
    else:
        # Kept so we can see what people ask for that we don't handle yet. Deleted after
        # UNRECOGNISED_KEEP_DAYS by the worker (purge_unrecognised, daily).
        note = ChangeLog(scope="user", scope_id=user.id, kind="unrecognised",
                         diff={"text": text.strip()[:1000]})
        db.add(note)
        if inbox.in_conversation(db, phone):
            # The team is talking to them: this goes to the team, not the usual menu
            db.commit()
            inbox.post(db, phone, text, "unrecognised")
            return ""
        topic = topic_reply(user, text)
        analytics.track(user, "message_not_understood", pointed_elsewhere=topic is not None)
        if topic is not None:  # something we can't do, but can point them the right way
            reply = Reply(topic[0])
            to_inbox = "unrecognised" if topic[1] else None
        else:
            # No reply yet: send_fallbacks answers once they've stopped typing (see FALLBACK_WAIT)
            note.diff = {**note.diff, "pending": True}
            db.commit()
            inbox.post(db, phone, text, "unrecognised")
            return ""
    if cmd in ("DONE", "SNOOZE", "WRONG") and len(words) == 1 and latest is not None:
        analytics.track(user, "reminder_replied", reply=cmd.lower())
    if cmd in ("DONE", "SNOOZE") and last and len(words) == 1:
        # The reminder's template couldn't carry the referral news: it rides on this reply.
        news = referral_news(db, user)
        if news:
            reply.text = f"{reply.text}\n\n{news[0]}"
            _mark_announced(news[1], datetime.now(ZoneInfo("UTC")))
    send_reply(db, phone, reply.text, kind="reply", buttons=reply.buttons,
               list_button=reply.list_button, sections=reply.sections)
    if reply.share:
        analytics.track(user, "share_message_sent", trigger="asked")
        real = get_provider().name == "meta"
        import time as _time
        if real:
            _time.sleep(0.7)
        send_message(db, phone, share_message(user), kind="share")
        if re.sub(r"\D", "", get_settings().meta_display_number):
            if real:
                _time.sleep(0.7)
            send_contact(db, phone, binjamin_card(), kind="share_card")
    if cmd == "DONE" and last and len(words) == 1:
        share_after_done(db, user, phone)
    db.commit()
    if to_inbox:
        inbox.post(db, phone, text, to_inbox)
    return reply.text
