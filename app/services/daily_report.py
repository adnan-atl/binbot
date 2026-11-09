"""The nightly growth report: at midnight (London) the worker posts how the day went to
SLACK_ANALYTICS_CHANNEL (#binjamin-analytics). Each number is compared with the same day last
week, so a quiet Sunday isn't mistaken for a bad one.

Where the numbers come from:
- Our database, for everything from sign-up onwards: sign-ups started and switched on, invites,
  reminders and replies, council lookups. It's the source of truth and ad blockers can't hide it.
- PostHog (POSTHOG_PERSONAL_API_KEY + POSTHOG_PROJECT_ID), for what happens before sign-up:
  visits, the website funnel, where visitors came from, share taps, pauses and deletions.
  Without those settings the report still goes out, with a note in place of those sections.

No resident is named or numbered in the report: counts and council names only.

Run by hand (prints, doesn't post):  python -m app.services.daily_report [YYYY-MM-DD]
"""
from __future__ import annotations

import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    ChangeLog,
    Council,
    CouncilStatus,
    Outbox,
    Property,
    Referral,
    Reminder,
    ReminderStatus,
    User,
    UserStatus,
)
from app.services.signup import SLOW_LOOKUP_SECONDS

log = logging.getLogger("daily_report")
LONDON = ZoneInfo("Europe/London")
K_WINDOW_DAYS = 28
SHARE_HOW = {"whatsapp": "WhatsApp", "copy_message": "copied message", "copy_link": "copied link"}

# The website funnel, in order: (event, label). Counted as unique visits (sessions).
FUNNEL = [
    ("$pageview", "Landed"),
    ("screen_viewed:postcode", "Postcode screen"),
    ("postcode_looked_up", "Council found"),
    ("calendar_loaded", "Bin days found"),
    ("calendar_confirmed", "Confirmed bins"),
    ("phone_added", "Phone added"),
    ("turn_on_tapped", "Tapped turn on reminders"),
]


def window(day: date) -> tuple[datetime, datetime]:
    """Midnight to midnight in London, as UTC (one day is 23 or 25 hours when the clocks change)."""
    start = datetime.combine(day, time(0), LONDON)
    end = datetime.combine(day + timedelta(days=1), time(0), LONDON)
    return start.astimezone(ZoneInfo("UTC")), end.astimezone(ZoneInfo("UTC"))


# ---- the database ---------------------------------------------------------------------------

@dataclass
class DbDay:
    started: int = 0
    started_invited: int = 0
    switched_on: int = 0
    switched_on_invited: int = 0
    stuck_at_hello: int = 0
    invite_clicks: int = 0
    referrals: int = 0
    share_messages: int = 0
    reminders_sent: int = 0
    reminders_failed: int = 0
    replies: Counter = field(default_factory=Counter)
    reminders_replied: int = 0
    reminder_collections: int = 0
    not_understood: int = 0
    # Alfred offers: of those who gave their number (and agreed to the terms) today, how many
    # left offers on vs opted out ("Don't tell me about other services" / "No feature updates")
    consented: int = 0
    offers_out: int = 0
    # ...and of those who switched on today
    on_offers_out: int = 0
    councils: dict = field(default_factory=dict)  # name -> problems Counter


def _council_of_round(db: Session, round_key: str) -> str | None:
    return db.scalar(select(Council.name).join(Property, Property.council_id == Council.id)
                     .where(Property.round_key == round_key).limit(1))


def _user_council(user: User | None) -> str | None:
    prop = user.property if user is not None else None
    return prop.council.name if prop is not None and prop.council is not None else None


def switched_on(db: Session, start: datetime, end: datetime) -> tuple[int, int]:
    """(switched on, of whom invited). One welcome goes out per sign-up that's finished."""
    phones = db.scalars(select(Outbox.to_phone).where(
        Outbox.kind == "welcome", Outbox.created_at >= start, Outbox.created_at < end)).all()
    phones = set(phones)
    invited = db.scalar(select(func.count()).select_from(User).where(
        User.phone_e164.in_(phones), User.referred_by_id.is_not(None))) if phones else 0
    return len(phones), invited or 0


def db_day(db: Session, start: datetime, end: datetime) -> DbDay:
    d = DbDay()
    new = db.scalars(select(User).where(User.created_at >= start, User.created_at < end)).all()
    d.started = len(new)
    d.started_invited = sum(1 for u in new if u.referred_by_id)
    d.switched_on, d.switched_on_invited = switched_on(db, start, end)
    gave = db.scalars(select(User).where(User.consent_at >= start, User.consent_at < end)).all()
    d.consented = len(gave)
    d.offers_out = sum(1 for u in gave if u.marketing_consent_at is None)
    on_phones = set(db.scalars(select(Outbox.to_phone).where(
        Outbox.kind == "welcome", Outbox.created_at >= start, Outbox.created_at < end)).all())
    if on_phones:
        d.on_offers_out = db.scalar(select(func.count()).select_from(User).where(
            User.phone_e164.in_(on_phones), User.marketing_consent_at.is_(None))) or 0
    d.stuck_at_hello = db.scalar(select(func.count()).select_from(User).where(
        User.hi_prompted_at >= start, User.hi_prompted_at < end,
        User.status == UserStatus.pending)) or 0

    def logged(kind):
        return db.scalars(select(ChangeLog).where(
            ChangeLog.kind == kind, ChangeLog.detected_at >= start, ChangeLog.detected_at < end)).all()

    d.invite_clicks = len(logged("ref_visit"))
    d.not_understood = len(logged("unrecognised"))
    d.referrals = db.scalar(select(func.count()).select_from(Referral).where(
        Referral.created_at >= start, Referral.created_at < end)) or 0
    d.share_messages = db.scalar(select(func.count()).select_from(Outbox).where(
        Outbox.kind.in_(("share", "welcome_share")), Outbox.created_at >= start,
        Outbox.created_at < end)) or 0

    # Reminders: one per collection a resident heard about (a snooze nudge isn't a second one)
    sent = db.scalars(select(Reminder).where(Reminder.sent_at >= start, Reminder.sent_at < end)).all()
    d.reminders_sent = len(sent)
    by_collection = defaultdict(set)
    for r in sent:
        by_collection[(r.user_id, r.collection_date)].add(r.reply)
    d.reminder_collections = len(by_collection)
    for replies in by_collection.values():
        got = [x for x in replies if x]
        if got:
            d.reminders_replied += 1
            d.replies[got[0]] += 1

    problems: dict[str, Counter] = defaultdict(Counter)
    failed = db.scalars(select(Reminder).where(
        Reminder.status == ReminderStatus.failed, Reminder.send_at >= start,
        Reminder.send_at < end)).all()
    d.reminders_failed = len(failed)
    for r in failed:
        problems[_user_council(db.get(User, r.user_id)) or "Unknown council"]["failed reminders"] += 1
    for u in new:  # council lookups during sign-up
        name = _user_council(u)
        if not name or u.lookup_source is None:
            continue
        problems[name]["lookups"] += 1
        if u.lookup_source == "manual":
            problems[name]["lookups failed"] += 1
        elif u.lookup_source == "neighbours":
            problems[name]["rescued by a neighbour"] += 1
        if (u.lookup_seconds or 0) > SLOW_LOOKUP_SECONDS:
            problems[name]["slow lookups"] += 1
    for row in logged("wrong_day"):
        problems[_council_of_round(db, row.scope_id) or "Unknown council"]["wrong-day votes"] += 1
    d.councils = problems
    return d


def k_factor(db: Session, end: datetime) -> tuple[float | None, int]:
    """Viral coefficient over the last K_WINDOW_DAYS: the share of new residents who came from an
    invite. In a steady state, total = organic + K x total, so K = invited / total. Returns
    (K, residents switched on in the window); K is None with fewer than 10 to go on."""
    total, invited = switched_on(db, end - timedelta(days=K_WINDOW_DAYS), end)
    return (invited / total if total >= 10 else None), total


# ---- PostHog --------------------------------------------------------------------------------

def _hogql(query: str) -> list[list]:
    s = get_settings()
    api = s.posthog_host.replace(".i.posthog.com", ".posthog.com")
    r = httpx.post(f"{api}/api/projects/{s.posthog_project_id}/query/", timeout=60,
                   headers={"Authorization": f"Bearer {s.posthog_personal_api_key}"},
                   json={"query": {"kind": "HogQLQuery", "query": query}})
    r.raise_for_status()
    return r.json().get("results") or []


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def posthog_day(start: datetime, end: datetime) -> dict | None:
    """Visits, funnel, sources, share taps and server events for one day. None when PostHog
    isn't set up for reading, or the query fails."""
    s = get_settings()
    if not (s.posthog_personal_api_key and s.posthog_project_id):
        return None
    span = f"timestamp >= toDateTime('{_ts(start)}') AND timestamp < toDateTime('{_ts(end)}')"
    try:
        counts = _hogql(
            # say_hello_tapped is the old name of turn_on_tapped (renamed Nov 2026): counted as one
            "SELECT multiIf(event = 'screen_viewed', concat('screen_viewed:', toString(properties.screen)), "
            "event = 'say_hello_tapped', 'turn_on_tapped', event) AS e, "
            "count(), uniq(properties.$session_id) "
            f"FROM events WHERE {span} GROUP BY e")
        sources = _hogql(
            "SELECT multiIf(properties.$current_url LIKE '%ref=%', 'invite link', "
            "notEmpty(toString(properties.utm_source)), concat('utm: ', toString(properties.utm_source)), "
            "notEmpty(toString(properties.$referring_domain)) AND properties.$referring_domain != '$direct', "
            "toString(properties.$referring_domain), 'direct') AS src, uniq(properties.$session_id) AS n "
            f"FROM events WHERE event = '$pageview' AND {span} GROUP BY src ORDER BY n DESC LIMIT 6")
        shares = _hogql(f"SELECT toString(properties.how), count() FROM events WHERE "
                        f"event = 'share_tapped' AND {span} GROUP BY 1 ORDER BY 2 DESC")
        failed = _hogql(f"SELECT toString(properties.council), count() FROM events WHERE "
                        f"event = 'calendar_failed' AND {span} GROUP BY 1")
        via = _hogql(f"SELECT toString(properties.via), count() FROM events WHERE "
                     f"event = 'reminders_switched_on' AND {span} GROUP BY 1")
        # Who tapped "Turn on reminders" on a phone or a computer, and whether they then switched
        # on (any time since: their message can come a while later)
        hello = _hogql(f"SELECT DISTINCT toString(person_id), toString(properties.on_computer) "
                       f"FROM events WHERE event IN ('turn_on_tapped', 'say_hello_tapped') AND {span}")
        # Which version of the site (design: oat = binjamin.uk, og = /d/og): visits from each,
        # and switch-ons by the version the person signed up on (person property signup_design;
        # empty for people who signed up entirely in WhatsApp)
        site_visits = _hogql(f"SELECT toString(properties.design), uniq(properties.$session_id) "
                             f"FROM events WHERE event = '$pageview' AND {span} GROUP BY 1")
        site_on = _hogql(f"SELECT toString(person.properties.signup_design), count() FROM events "
                         f"WHERE event = 'reminders_switched_on' AND {span} GROUP BY 1")
        on = {p for (p,) in _hogql(
            "SELECT DISTINCT toString(person_id) FROM events WHERE event = 'reminders_switched_on' "
            f"AND timestamp >= toDateTime('{_ts(start)}')")}
    except Exception:
        log.warning("daily report: PostHog query failed", exc_info=True)
        return None
    by_device: dict[str, list[int]] = {}
    for person, computer in hello:
        dev = by_device.setdefault("computer" if computer in ("true", "1") else "phone", [0, 0])
        dev[0] += 1
        dev[1] += person in on
    events = {e: int(n) for e, n, _ in counts}
    sessions = {e: int(u) for e, _, u in counts}
    return {"events": events, "sessions": sessions, "sources": [(a, int(b)) for a, b in sources],
            "shares": {SHARE_HOW.get(a, a or "?"): int(b) for a, b in shares},
            "calendar_failed": {a or "Unknown council": int(b) for a, b in failed},
            "via": {a or "?": int(b) for a, b in via}, "hello": by_device,
            "sites": _sites(site_visits, site_on)}


def _sites(visits: list, on: list) -> dict[str, list[int]]:
    """{site: [visits, switched on]} with readable names; WhatsApp-only sign-ups have no site."""
    names = {"oat": "binjamin.uk", "og": "/d/og", "": "WhatsApp only"}
    out: dict[str, list[int]] = {}
    for d, n in visits:
        out.setdefault(names.get(d or "", d), [0, 0])[0] += int(n)
    for d, n in on:
        out.setdefault(names.get(d or "", d), [0, 0])[1] += int(n)
    return out


# ---- the message ----------------------------------------------------------------------------

def _pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "–"


def _vs(now: int, before: int) -> str:
    """'▲ 20%' against the same day last week."""
    if not before:
        return "" if not now else " _(new)_"
    change = round(100 * (now - before) / before)
    if change == 0:
        return " _(=)_"
    return f" _({'▲' if change > 0 else '▼'} {abs(change)}%)_"


def build(db: Session, day: date) -> str:
    s = get_settings()
    start, end = window(day)
    wstart, wend = window(day - timedelta(days=7))
    d, w = db_day(db, start, end), db_day(db, wstart, wend)
    ph, phw = posthog_day(start, end), posthog_day(wstart, wend)
    active = db.scalar(select(func.count()).select_from(User).where(User.status == UserStatus.active)) or 0
    k, k_base = k_factor(db, end)
    brand = s.brand_name
    title = (f"*{brand} daily report: {day.strftime('%a %-d %b %Y')}*  "
             f"_(compared with {(day - timedelta(days=7)).strftime('%a %-d %b')})_")
    lines = [title, ""]

    # Headline
    visits = ph["sessions"].get("$pageview", 0) if ph else None
    visits_w = phw["sessions"].get("$pageview", 0) if phw else 0
    lines.append("*Headline*")
    if visits is not None:
        lines.append(f"• Visits: *{visits}*{_vs(visits, visits_w)}")
    lines.append(f"• Sign-ups started: *{d.started}*{_vs(d.started, w.started)}")
    lines.append(f"• Switched on: *{d.switched_on}*{_vs(d.switched_on, w.switched_on)}"
                 + (f"  ·  visit → switched on: *{_pct(d.switched_on, visits)}*" if visits else ""))
    if ph and ph["via"]:
        lines.append("   by " + ", ".join(f"{k_} {n}" for k_, n in sorted(ph["via"].items())))
    if ph and ph.get("sites"):
        lines.append("• By site: " + ", ".join(
            f"{site} {v} visits → {n_on} switched on" if site != "WhatsApp only" else f"{site} {n_on} switched on"
            for site, (v, n_on) in sorted(ph["sites"].items(), key=lambda kv: -kv[1][0])))
    lines.append(f"• Active residents now: *{active}*")
    if d.stuck_at_hello:
        lines.append(f"• Reached the last step but haven't messaged yet: {d.stuck_at_hello}")
    if ph and ph.get("hello"):
        lines.append("• Tapped turn on reminders → switched on: " + ", ".join(
            f"{dev} {n_on}/{n} ({_pct(n_on, n)})" for dev, (n, n_on) in sorted(ph["hello"].items())))
    lines.append("")

    # Alfred offers: the opt-out on the phone step (website) / consent step (WhatsApp)
    lines.append(f"*{s.parent_name} offers*")
    if d.consented:
        lines.append(f"• Opted out: *{d.offers_out}* of {d.consented} who gave their number "
                     f"(*{_pct(d.offers_out, d.consented)}*)"
                     + (f"  _(last week {_pct(w.offers_out, w.consented)})_" if w.consented else ""))
    else:
        lines.append("• Nobody gave their number today")
    if d.switched_on:
        lines.append(f"• Of today's switch-ons: {d.switched_on - d.on_offers_out} can hear about "
                     f"{s.parent_name}, {d.on_offers_out} opted out")
    reachable = db.scalar(select(func.count()).select_from(User).where(
        User.status == UserStatus.active, User.marketing_consent_at.is_not(None))) or 0
    lines.append(f"• All active residents: *{reachable}* of {active} can hear about "
                 f"{s.parent_name} (*{_pct(reachable, active)}*)")
    if ph and ph["events"].get("hi_from_unexpected_number"):
        lines.append(f"• \"Hi\" from a number nobody typed: {ph['events']['hi_from_unexpected_number']}")
    lines.append("")

    # Funnel
    lines.append("*Website funnel* _(unique visits)_")
    if ph:
        steps = [(label, ph["sessions"].get(ev, 0)) for ev, label in FUNNEL]
        prev, worst = None, None
        for label, n in steps:
            rate = f" ({_pct(n, prev)})" if prev is not None else ""
            lines.append(f"• {label}: {n}{rate}")
            if prev and (worst is None or (prev - n) / prev > worst[0]):
                worst = ((prev - n) / prev, label)
            prev = n
        if worst and worst[0] > 0:
            lines.append(f"Biggest drop: *{worst[1]}* (lost {round(worst[0] * 100)}% of the step before)")
        if ph["sources"]:
            lines.append("*Where visits came from*: " + ", ".join(f"{a} {b}" for a, b in ph["sources"]))
    else:
        lines.append("_Not available: set POSTHOG_PERSONAL_API_KEY and POSTHOG_PROJECT_ID on the server._")
    lines.append("")

    # Sharing and K-factor
    lines.append("*Sharing and K-factor*")
    lines.append(f"• Invite link clicks: {d.invite_clicks}{_vs(d.invite_clicks, w.invite_clicks)}  ·  "
                 f"invited sign-ups: {d.started_invited}  ·  invited switched on: {d.switched_on_invited}")
    taps = ("  ·  share taps on the site: "
            + (", ".join(f"{a} {b}" for a, b in ph["shares"].items()) or "none")) if ph else ""
    lines.append(f"• Share messages sent in WhatsApp: {d.share_messages}{taps}")
    lines.append(f"• From an invite today: {_pct(d.switched_on_invited, d.switched_on)} of switch-ons"
                 f"  ·  referrals credited: {d.referrals}")
    if k is None:
        lines.append(f"• K-factor ({K_WINDOW_DAYS} days): not enough data yet ({k_base} switched on)")
    else:
        lines.append(f"• K-factor ({K_WINDOW_DAYS} days): *{k:.2f}* — every 100 new residents bring in "
                     f"about {round(k * 100)} through invites ({k_base} switched on)")
    lines.append("")

    # Engagement
    lines.append("*Engagement*")
    lines.append(f"• Reminders sent: {d.reminders_sent}{_vs(d.reminders_sent, w.reminders_sent)}"
                 + (f"  ·  failed: *{d.reminders_failed}*" if d.reminders_failed else ""))
    if d.reminder_collections:
        detail = ", ".join(f"{k_.capitalize()} {n}" for k_, n in d.replies.most_common())
        lines.append(f"• Replied to: {_pct(d.reminders_replied, d.reminder_collections)}"
                     + (f" ({detail})" if detail else ""))
    extra = [f"not understood {d.not_understood}"]
    if ph:
        ev = ph["events"]
        extra = [f"paused {ev.get('reminders_paused', 0)}", f"deleted {ev.get('account_deleted', 0)}",
                 f"settings changes {ev.get('reminder_time_changed', 0) + ev.get('bin_switched_on', 0) + ev.get('bin_switched_off', 0)}",
                 *extra, f"asked for a human {ev.get('asked_for_human', 0)}"]
    lines.append("• " + "  ·  ".join(extra)[0].upper() + "  ·  ".join(extra)[1:])
    lines.append("")

    # Problem councils
    lines.append("*Problem councils*")
    problems = defaultdict(Counter, {k_: Counter(v) for k_, v in d.councils.items()})
    if ph:
        for name, n in ph["calendar_failed"].items():
            problems[name]["bin days not found on the site"] += n
    flagged = []
    for name, c in problems.items():
        bad = {k_: n for k_, n in c.items() if k_ not in ("lookups", "rescued by a neighbour")}
        if not bad:
            continue
        score = sum(bad.values())
        what = ", ".join(f"{n} {k_}" for k_, n in sorted(bad.items(), key=lambda x: -x[1]))
        of = f" (of {c['lookups']} lookups)" if c.get("lookups") else ""
        flagged.append((score, f"• *{name}*: {what}{of}"))
    for _, line in sorted(flagged, key=lambda x: -x[0])[:8]:
        lines.append(line)
    # Only councils someone relies on: the nightly check only re-tests those, so a council whose
    # one failed sign-up never came back would otherwise stay "failing" here for ever.
    served = select(Property.council_id).join(User, User.property_id == Property.id).where(
        User.status == UserStatus.active)
    broken = db.scalars(select(Council.name).where(
        Council.status.in_((CouncilStatus.broken, CouncilStatus.degraded)),
        Council.id.in_(served)).order_by(Council.name)).all()
    if broken:
        lines.append(f"• Health check failing: {', '.join(broken[:12])}"
                     + (f" and {len(broken) - 12} more" if len(broken) > 12 else ""))
    if not flagged and not broken:
        lines.append("• None today")
    return "\n".join(lines)


def post(db: Session, day: date | None = None) -> bool:
    """Post the report for `day` (default: yesterday in London) to SLACK_ANALYTICS_CHANNEL."""
    from app.services import inbox

    s = get_settings()
    if not inbox.enabled() or not s.slack_analytics_channel:
        return False
    day = day or (datetime.now(LONDON).date() - timedelta(days=1))
    text = build(db, day)
    res = inbox._api("chat.postMessage", channel=s.slack_analytics_channel, text=text,
                     unfurl_links=False, mrkdwn=True)
    if not res.get("ok") and res.get("error") in ("not_in_channel", "channel_not_found"):
        # A public channel the app hasn't been added to: join it (needs channels:join), retry
        found = inbox._get("conversations.list", types="public_channel", limit=1000)
        cid = next((c["id"] for c in found.get("channels", [])
                    if c.get("name") == s.slack_analytics_channel.lstrip("#")), None)
        if cid and inbox._api("conversations.join", channel=cid).get("ok"):
            res = inbox._api("chat.postMessage", channel=cid, text=text, unfurl_links=False, mrkdwn=True)
    return bool(res.get("ok"))


def run(db: Session) -> bool:
    """The worker's midnight job."""
    return post(db)


if __name__ == "__main__":
    from app.core.db import session_factory

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    the_day = date.fromisoformat(arg) if arg else datetime.now(LONDON).date() - timedelta(days=1)
    session = session_factory()()
    try:
        print(build(session, the_day))
    finally:
        session.close()
