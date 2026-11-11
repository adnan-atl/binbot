"""
Sign-up steps shared by the website (api/routes.py) and the WhatsApp sign-up (services/wa_signup.py).
Both go through these functions, so any change to how an address becomes a calendar, which bins
get seeded, or what turns reminders on applies to both.
"""
import logging
import secrets
from time import monotonic
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.base import unreachable
from app.adapters.manual import expand_rule
from app.api import schemas as S
from app.core.config import get_settings
from app.core.db import session_factory
from app.models import (
    BinCategory,
    ChangeLog,
    CouncilStatus,
    Property,
    User,
    UserBinSetting,
    UserStatus,
)
from app.services.normalize import describe_rule, display_label, is_daily
from app.services.reminders import credit_referral, rebuild_reminders
from app.services.schedule import refresh_round, resolve_property, round_has_calendar, schedule_for
from app.services.verify import rescue_round, verify_round

log = logging.getLogger(__name__)


# A council lookup slower than this (or failing) pings the team in Slack: someone was left
# watching the spinner.
SLOW_LOOKUP_SECONDS = 20


@dataclass
class StartResult:
    user: User
    prop: Property
    source: str  # demo | council | neighbours | manual
    error: str | None
    shared: bool  # reused a neighbour's calendar: no council lookup happened


def new_referral_code() -> str:
    return secrets.token_urlsafe(6)[:8].lower()


def start_signup(db: Session, postcode: str, uprn: str | None = None,
                 house_number: str | None = None, address_line: str | None = None,
                 referral_code: str | None = None, user: User | None = None) -> StartResult:
    """
    Address in, calendar out: find or create the property, fetch its round from the council
    (or reuse a neighbour's), and seed one bin setting per council bin. Creates the user, or
    attaches the property to `user` (WhatsApp sign-up already has one, holding the number).
    Raises geo.BadPostcode. Commits.
    """
    prop = resolve_property(db, postcode, uprn, house_number, address_line)
    council = prop.council
    if user is None:
        user = User(referral_code=new_referral_code())
        db.add(user)
    user.property_id = prop.id
    user.property = prop
    if referral_code and not user.referred_by_id:
        ref = db.scalar(select(User).where(User.referral_code == referral_code))
        if ref and ref.id != user.id:
            user.referred_by_id = ref.id
    db.flush()

    started = monotonic()
    source, error, shared = "manual", None, False
    if get_settings().demo_mode:
        source = "demo"
    if round_has_calendar(db, prop.round_key):
        # A neighbour already fetched this postcode's round: reuse it, no lookup at all.
        source, shared = "neighbours", True
    elif copy_neighbour_manual(db, user, prop):
        # The council can't be asked (or wasn't answering) and a neighbour typed their days in
        # by hand: same postcode, same lorry, so they're this home's days too.
        source, shared = "neighbours", True
    elif council.status != CouncilStatus.unsupported:
        res = refresh_round(db, prop, source="demo" if source == "demo" else "council")
        # A site that isn't answering won't answer for the neighbours either: don't make the
        # resident wait through two more timeouts.
        if res.error and not unreachable(res.error) and rescue_round(db, prop):
            source = "neighbours"  # this address failed; a neighbour's calendar worked
        elif res.error:
            error = res.error
        else:
            source = source if source == "demo" else "council"
    if not error:
        seed_settings(db, user, prop)
    db.commit()
    if not error and not shared:  # neighbours reuse a round that was already checked
        log_checks(db, prop)
    db.commit()
    secs = monotonic() - started
    user.lookup_seconds, user.lookup_source = round(secs, 1), "manual" if error else source
    db.commit()
    if source == "council" and not error:
        # A fresh lookup means nobody nearby is set up yet: get the neighbouring postcodes'
        # calendars ready in the background, so the rest of the street loads instantly.
        from app.services import prefetch

        prefetch.start(prop.id)
    if not shared and (error or secs > SLOW_LOOKUP_SECONDS):
        from app.services import inbox  # Slack: we look into it

        inbox.alert_slow_lookup(prop, secs, source, error)
    return StartResult(user=user, prop=prop, source=source if not error else "manual",
                       error=error, shared=shared)


def copy_neighbour_manual(db: Session, user: User, prop) -> bool:
    """Give `user` the bins a neighbour on the same round entered by hand. The most recent
    neighbour wins (their days are the freshest). False when nobody has typed any in."""
    rows = db.execute(
        select(UserBinSetting, User.id)
        .join(User, User.id == UserBinSetting.user_id)
        .join(Property, Property.id == User.property_id)
        .where(Property.round_key == prop.round_key, User.id != user.id,
               User.status != UserStatus.stopped,
               UserBinSetting.bin_type_id.is_(None), UserBinSetting.override_rule.is_not(None))
        .order_by(User.created_at.desc())
    ).all()
    if not rows:
        return False
    donor = rows[0][1]
    for s, uid in rows:
        if uid != donor or not s.enabled:
            continue
        db.add(UserBinSetting(user_id=user.id, label=s.label, category=s.category,
                              override_rule=dict(s.override_rule), colour=s.colour))
    db.flush()
    return True


def log_checks(db: Session, prop) -> None:
    """Checker results are for ops, never shown to the resident: log them for review."""
    _, found = schedule_for(db, prop, with_checks=True)
    for c in found:
        db.add(ChangeLog(scope="round", scope_id=prop.round_key,
                         kind="data_conflict" if c["kind"] == "conflict" else "missing_stream",
                         diff=c))


# A council bin starts unticked for new sign-ups once most of that council's residents have
# unticked it (garden waste is a paid extra in most places; some councils list every service
# for every address). The site shows "Which bins do you put out?" so they can tick it back.
UNUSED_MIN_RESIDENTS = 10
UNUSED_SHARE = 0.5


def mostly_unused(db: Session, bin_type_ids: list[str]) -> set[str]:
    """Bin types that more than UNUSED_SHARE of at least UNUSED_MIN_RESIDENTS active residents
    have switched off."""
    if not bin_type_ids:
        return set()
    rows = db.execute(
        select(UserBinSetting.bin_type_id, UserBinSetting.enabled, func.count())
        .join(User, User.id == UserBinSetting.user_id)
        .where(UserBinSetting.bin_type_id.in_(bin_type_ids), User.status == UserStatus.active)
        .group_by(UserBinSetting.bin_type_id, UserBinSetting.enabled)).all()
    on: dict[str, int] = {}
    off: dict[str, int] = {}
    for bt, enabled, n in rows:
        (on if enabled else off)[bt] = n
    return {bt for bt, n in off.items()
            if n + on.get(bt, 0) >= UNUSED_MIN_RESIDENTS and n > UNUSED_SHARE * (n + on.get(bt, 0))}


def seed_settings(db: Session, user: User, prop) -> None:
    """One UserBinSetting per council bin on the property's round."""
    for s in db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == user.id,
                                                      UserBinSetting.bin_type_id.is_not(None),
                                                      UserBinSetting.override_rule.is_(None))):
        db.delete(s)
    db.flush()
    found = schedule_for(db, prop)
    unused = mostly_unused(db, [b["bin_type_id"] for b in found])
    for b in found:
        db.add(UserBinSetting(user_id=user.id, bin_type_id=b["bin_type_id"],
                              label=b["label"], category=BinCategory(b["category"]),
                              enabled=b["bin_type_id"] not in unused))
    db.flush()


def fill_gaps(db: Session, user: User) -> None:
    """If the checker found a gap, ask the council about a neighbour or two on the same
    postcode and fill it (re-seeding the bins). Quick enough to wait for."""
    started = monotonic()
    if user.property and verify_round(db, user.property, confirm=False)["filled"]:
        seed_settings(db, user, user.property)
    if user.lookup_seconds is not None:  # the loader waited for this too
        user.lookup_seconds = round(user.lookup_seconds + monotonic() - started, 1)


def confirm_round(property_id: str) -> None:
    """Background: confirm the round with a neighbour (at most once a week per round). Doesn't
    change what the resident sees, so nobody waits for it."""
    db = session_factory()()
    try:
        prop = db.get(Property, property_id)
        if prop is not None:
            verify_round(db, prop)
            db.commit()
    except Exception:  # never surfaces to the resident; ops see it in the logs
        log.exception("confirm_round failed for %s", property_id)
        db.rollback()
    finally:
        db.close()


def activate(db: Session, user: User) -> int:
    """Turn reminders on: schedule them and credit whoever referred this resident. The caller
    sends the welcome. Returns how many reminders are scheduled."""
    user.status = UserStatus.active
    user.start_code = None
    n = rebuild_reminders(db, user)
    credit_referral(db, user)
    return n


# Councils ask for bins out by about 7am, so after that today's collection has as good as gone:
# from then on "next" means the next one after today.
SAME_DAY_CUTOFF = time(7, 0)


def first_showable_day(now: datetime | None = None) -> date:
    """Today until SAME_DAY_CUTOFF (London time), then tomorrow."""
    now = now or datetime.now(ZoneInfo(get_settings().timezone))
    return now.date() if now.time() < SAME_DAY_CUTOFF else now.date() + timedelta(days=1)


def bins_out(db: Session, user: User, now: datetime | None = None) -> list[S.BinOut]:
    """The resident's bins with their days and next dates, as both sign-ups show them."""
    first = first_showable_day(now).isoformat()
    council_bins = {b["bin_type_id"]: b for b in schedule_for(db, user.property)} if user.property else {}
    out = []
    for s in db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == user.id)):
        colours: list[str] = []
        if s.override_rule:
            dates = expand_rule(s.override_rule, date.today(), 60)
            rule = describe_rule(dates)
            rule["days"] = s.override_rule.get("days", rule["days"])
            rule["frequency"] = s.override_rule.get("frequency", rule["frequency"])
            upcoming = [d.isoformat() for d in dates if d.isoformat() >= first][:4]
            colour, manual, options = None, True, []
            rule["anchor"] = s.override_rule.get("anchor")
            rule["moves"] = s.override_rule.get("moves") or {}
        elif s.bin_type_id in council_bins:
            cb = council_bins[s.bin_type_id]
            rule = {"days": cb["days"], "frequency": cb["frequency"], "next": cb["next"],
                    "purpose": cb["purpose"], "frequency_source": cb.get("frequency_source", "")}
            # The round's real dates (bank holiday moves included), as schedule_for has them
            upcoming = [d for d in cb["dates"] if d >= first][:4]
            colour, manual, options = cb["colour"], False, cb.get("colour_options", [])
            colours = cb.get("colours", [])
        else:
            rule, colour, manual, options = {"days": [], "frequency": "irregular", "next": None}, None, True, []
            upcoming = []
        if upcoming:  # today's collection after the cutoff isn't "next" any more
            rule["next"] = upcoming[0]
        # Council bins get the resident-facing name ("General waste (black bin)"); bins they
        # added or renamed themselves keep their own words
        label = s.label if manual else display_label(s.label, s.category.value)
        out.append(S.BinOut(setting_id=s.id, bin_type_id=s.bin_type_id, label=label,
                            category=s.category.value, colour=s.colour or colour, enabled=s.enabled,
                            colour_options=[] if (s.colour or colour) else options,
                            colours=[] if s.colour else colours,
                            manual=manual, upcoming=upcoming, daily=is_daily(rule["days"]), **rule))
    return out
