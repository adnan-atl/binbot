import logging
import re
import secrets
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.manual import expand_rule
from app.adapters.registry import coverage, spec_for
from app.providers.messaging import send_message
from app.api import schemas as S
from app.core.config import get_settings
from app.core.db import get_db, session_factory
from app.core.guard import client_ip, human_check_on, issue_pass, rate_limit, require_human, verify_turnstile
from app.models import (
    BinCategory,
    ChangeLog,
    CouncilStatus,
    Referral,
    Reminder,
    ReminderMode,
    User,
    UserBinSetting,
    UserStatus,
    level_for,
    now,
)
from app.services import geo, split
from app.services.normalize import describe_rule
from app.services.reminders import handle_inbound, rebuild_reminders, start_link, welcome_message
from app.services.verify import rescue_round, verify_round
from app.services.schedule import (
    get_or_create_council,
    own_round,
    postcode_round_key,
    refresh_round,
    representative_for,
    resolve_property,
    round_has_calendar,
    schedule_for,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _user(db: Session, user_id: str) -> User:
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    u.last_active_at = datetime.now(timezone.utc)  # saved with whatever the request commits
    return u


# ---- step 1: postcode -------------------------------------------------------------


@router.post("/human")
def human(body: S.HumanIn, request: Request):
    """Turnstile token in, a 30-minute pass out (sent back as X-Human-Pass on the sign-up calls)."""
    rate_limit(request, "human", 20, 600)
    if not human_check_on():
        return {"pass": ""}
    if not verify_turnstile(body.token, client_ip(request)):
        raise HTTPException(403, "The quick check didn't go through. Refresh the page and try again.")
    return {"pass": issue_pass()}


@router.get("/postcode/{postcode}", response_model=S.PostcodeOut)
def postcode_lookup(postcode: str, request: Request, addresses: bool = False,
                    db: Session = Depends(get_db)):
    """
    What we know about a postcode. `addresses=true` runs the (paid) address lookup; the
    UI only asks for it when the council needs an address AND no neighbour has signed up
    yet (round_known=false). Otherwise the first neighbour's calendar is reused for free.
    """
    rate_limit(request, "postcode", 60, 600)
    if addresses:
        require_human(request)
        rate_limit(request, "addresses", 10, 3600)
    try:
        info = geo.lookup_postcode(postcode)
    except geo.BadPostcode:
        raise HTTPException(400, "That doesn't look like a UK postcode")
    spec = spec_for(info.gss_code, info.district)
    council = get_or_create_council(db, info.gss_code, info.district)
    known = round_has_calendar(db, postcode_round_key(council, info.postcode))
    db.commit()
    return S.PostcodeOut(
        postcode=info.postcode, council=info.district, gss_code=info.gss_code,
        supported=spec is not None,
        needs_house_number=bool(spec and (spec.needs_house_number or spec.needs_uprn)),
        needs_uprn=bool(spec and spec.needs_uprn),
        round_known=known,
        addresses=[S.AddressOut(**a.__dict__) for a in geo.lookup_addresses(info.postcode)]
        if addresses else [],
    )


# ---- step 2: resolve calendar ------------------------------------------------------


@router.post("/signup/start", response_model=S.StartOut)
def signup_start(body: S.StartIn, request: Request, db: Session = Depends(get_db)):
    require_human(request)
    rate_limit(request, "signup", 8, 600)  # each sign-up can mean a council lookup
    try:
        prop = resolve_property(db, body.postcode, body.uprn, body.house_number, body.address_line)
    except geo.BadPostcode:
        raise HTTPException(400, "That doesn't look like a UK postcode")
    council = prop.council

    user = User(property_id=prop.id, referral_code=secrets.token_urlsafe(6)[:8].lower())
    if body.referral_code:
        ref = db.scalar(select(User).where(User.referral_code == body.referral_code))
        if ref:
            user.referred_by_id = ref.id
    db.add(user)
    db.flush()

    source, error, shared = "manual", None, False
    if get_settings().demo_mode:
        source = "demo"
    if round_has_calendar(db, prop.round_key):
        # A neighbour already fetched this postcode's round: reuse it, no lookup at all.
        source, shared = "neighbours", True
    elif council.status != CouncilStatus.unsupported:
        res = refresh_round(db, prop, source="demo" if source == "demo" else "council")
        if res.error and rescue_round(db, prop):
            source = "neighbours"  # this address failed; a neighbour's calendar worked
        elif res.error:
            error = res.error
        else:
            source = source if source == "demo" else "council"
    if not error:
        _seed_settings(db, user, prop)
    if user.referred_by_id:
        db.add(Referral(referrer_id=user.referred_by_id, referred_id=user.id))
    db.commit()
    if not error and not shared:  # neighbours reuse a round that was already checked
        _log_checks(db, prop)
    db.commit()
    return S.StartOut(user_id=user.id, property_id=prop.id, council=council.name,
                      source=source if not error else "manual", error=error, shared=shared,
                      bins=_bins_out(db, user))


def _log_checks(db: Session, prop) -> None:
    """Checker results are for ops, never shown to the resident: log them for review."""
    _, found = schedule_for(db, prop, with_checks=True)
    for c in found:
        db.add(ChangeLog(scope="round", scope_id=prop.round_key,
                         kind="data_conflict" if c["kind"] == "conflict" else "missing_stream",
                         diff=c))


def _seed_settings(db: Session, user: User, prop) -> None:
    """One UserBinSetting per council bin on the property's round."""
    for s in db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == user.id,
                                                      UserBinSetting.bin_type_id.is_not(None),
                                                      UserBinSetting.override_rule.is_(None))):
        db.delete(s)
    db.flush()
    for b in schedule_for(db, prop):
        db.add(UserBinSetting(user_id=user.id, bin_type_id=b["bin_type_id"],
                              label=b["label"], category=BinCategory(b["category"])))
    db.flush()


@router.post("/users/{user_id}/own-calendar", response_model=S.StartOut)
def own_calendar(user_id: str, body: S.OwnCalendarIn, request: Request,
                 db: Session = Depends(get_db)):
    """
    'These aren't my days': detach this home from the postcode round and fetch its own
    calendar. This is the one place a paid address lookup happens for a non-first neighbour.
    """
    require_human(request)
    rate_limit(request, "own-calendar", 5, 600)
    user = _user(db, user_id)
    prop = user.property
    if prop is None:
        raise HTTPException(400, "No property on this user")
    before = (prop.round_key, prop.uprn, prop.house_number, prop.address_line)
    if body.uprn:
        prop.uprn, prop.house_number, prop.address_line = body.uprn, body.house_number, body.address_line
    elif body.house_number:
        prop.house_number, prop.address_line = body.house_number, body.address_line
    own_round(db, prop)
    source = "demo" if get_settings().demo_mode else "council"
    res = refresh_round(db, prop, source=source)
    if res.error:
        # Never leave someone worse off than before: go back to the street's calendar.
        prop.round_key, prop.uprn, prop.house_number, prop.address_line = before
        db.commit()
        return S.StartOut(user_id=user.id, property_id=prop.id, council=prop.council.name,
                          source="neighbours", shared=True, bins=_bins_out(db, user),
                          error="We couldn't find a separate calendar for that address, so "
                                "you're still on your street's days.")
    _seed_settings(db, user, prop)
    _log_checks(db, prop)
    split.own_calendar_vote(db, before[0], prop)
    db.commit()
    return S.StartOut(user_id=user.id, property_id=prop.id, council=prop.council.name,
                      source=source, shared=False, bins=_bins_out(db, user))


@router.post("/users/{user_id}/verify", response_model=list[S.BinOut])
def verify(user_id: str, background: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Runs while the loader is on screen: if the checker found a gap, ask the council about
    a neighbour or two on the same postcode and fill it. Returns the bins to show.
    Confirming the round with a neighbour doesn't change what's shown, so it runs after the
    response and the resident never waits for it.
    """
    user = _user(db, user_id)
    if user.property and verify_round(db, user.property, confirm=False)["filled"]:
        _seed_settings(db, user, user.property)
    db.commit()
    out = _bins_out(db, user)
    if user.property:
        background.add_task(_confirm_round, user.property.id)
    return out


def _confirm_round(property_id: str) -> None:
    """Background: confirm the round with a neighbour (at most once a week per round)."""
    from app.models import Property

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


# Councils ask for bins out by about 7am, so after that today's collection has as good as gone:
# from then on "next" means the next one after today.
SAME_DAY_CUTOFF = time(7, 0)


def _first_showable_day(now: datetime | None = None) -> date:
    """Today until SAME_DAY_CUTOFF (London time), then tomorrow."""
    now = now or datetime.now(ZoneInfo(get_settings().timezone))
    return now.date() if now.time() < SAME_DAY_CUTOFF else now.date() + timedelta(days=1)


def _bins_out(db: Session, user: User, now: datetime | None = None) -> list[S.BinOut]:
    first = _first_showable_day(now).isoformat()
    council_bins = {b["bin_type_id"]: b for b in schedule_for(db, user.property)} if user.property else {}
    out = []
    for s in db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == user.id)):
        if s.override_rule:
            dates = expand_rule(s.override_rule, date.today(), 60)
            rule = describe_rule(dates)
            rule["days"] = s.override_rule.get("days", rule["days"])
            rule["frequency"] = s.override_rule.get("frequency", rule["frequency"])
            upcoming = [d.isoformat() for d in dates if d.isoformat() >= first][:4]
            colour, manual = None, True
        elif s.bin_type_id in council_bins:
            cb = council_bins[s.bin_type_id]
            rule = {"days": cb["days"], "frequency": cb["frequency"], "next": cb["next"],
                    "purpose": cb["purpose"], "frequency_source": cb.get("frequency_source", "")}
            # The round's real dates (bank holiday moves included), as schedule_for has them
            upcoming = [d for d in cb["dates"] if d >= first][:4]
            colour, manual = cb["colour"], False
        else:
            rule, colour, manual = {"days": [], "frequency": "irregular", "next": None}, None, True
            upcoming = []
        if upcoming:  # today's collection after the cutoff isn't "next" any more
            rule["next"] = upcoming[0]
        out.append(S.BinOut(setting_id=s.id, bin_type_id=s.bin_type_id, label=s.label,
                            category=s.category.value, colour=colour, enabled=s.enabled,
                            manual=manual, upcoming=upcoming, **rule))
    return out


# ---- step 3: confirm / edit bins ---------------------------------------------------


@router.get("/users/{user_id}/bins", response_model=list[S.BinOut])
def get_bins(user_id: str, db: Session = Depends(get_db)):
    return _bins_out(db, _user(db, user_id))


@router.put("/users/{user_id}/bins", response_model=list[S.BinOut])
def put_bins(user_id: str, body: S.BinsIn, db: Session = Depends(get_db)):
    user = _user(db, user_id)
    for sid in body.removed:
        s = db.get(UserBinSetting, sid)
        if s and s.user_id == user.id:
            db.delete(s)
    for b in body.bins:
        s = db.get(UserBinSetting, b.setting_id) if b.setting_id else None
        if s is None or s.user_id != user.id:
            s = UserBinSetting(user_id=user.id)
            db.add(s)
        s.label, s.category, s.enabled = b.label, BinCategory(b.category), b.enabled
        if b.days is not None:
            s.override_rule = {"days": b.days, "frequency": b.frequency or "weekly",
                               "anchor": b.anchor}
        elif b.setting_id is None:
            s.override_rule = None
    db.flush()
    rebuild_reminders(db, user)
    db.commit()
    return _bins_out(db, user)


# ---- step 4: phone -----------------------------------------------------------------


def _e164(raw: str) -> str:
    d = re.sub(r"[^\d+]", "", raw)
    if d.startswith("+44"):
        d = "0" + d[3:]
    elif d.startswith("44"):
        d = "0" + d[2:]
    elif not d.startswith("0"):
        d = "0" + d
    if not re.fullmatch(r"07\d{9}", d):
        raise HTTPException(400, "Enter a UK mobile number")
    return "+44" + d[1:]


@router.post("/users/{user_id}/phone")
def set_phone(user_id: str, body: S.PhoneIn, db: Session = Depends(get_db)):
    user = _user(db, user_id)
    phone = _e164(body.phone)
    clash = db.scalar(select(User).where(User.phone_e164 == phone, User.id != user.id))
    if clash and get_settings().meta_display_number and clash.status == UserStatus.pending:
        clash.phone_e164 = None  # typed but never proven; whoever says hi from it owns it
        db.flush()
    elif clash:
        raise HTTPException(409, "That number already has reminders set up")
    user.phone_e164 = phone
    if body.consent:
        user.consent_at = now()
    if body.marketing_consent:
        user.marketing_consent_at = now()
    db.commit()
    return {"phone": phone}


# ---- step 5: reminder prefs (turns reminders on) -----------------------------------


@router.post("/users/{user_id}/reminders")
def set_prefs(user_id: str, body: S.PrefsIn, db: Session = Depends(get_db)):
    user = _user(db, user_id)
    if not user.phone_e164:
        raise HTTPException(400, "Add a WhatsApp number first")
    try:
        user.reminder_mode = ReminderMode(body.mode)
    except ValueError:
        raise HTTPException(400, "mode must be night_before or morning_of")
    if not re.fullmatch(r"\d{2}:\d{2}", body.time):
        raise HTTPException(400, "time must be HH:MM")
    user.reminder_time = body.time
    was_active = user.status == UserStatus.active
    if not was_active and get_settings().meta_display_number:
        # Don't message a typed number cold: the resident says hi to us from their WhatsApp
        # first (handle_inbound), which proves the number and starts their reminders.
        if not user.start_code:
            user.start_code = _new_start_code(db)
        db.commit()
        return {"status": "awaiting_hi", "scheduled": 0, "start_code": user.start_code,
                "start_link": start_link(user.start_code)}
    user.status = UserStatus.active
    n = rebuild_reminders(db, user)
    if not was_active:
        send_message(db, user.phone_e164, welcome_message(db, user), kind="welcome")
    db.commit()
    return {"status": "active", "scheduled": n}


_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


def _new_start_code(db: Session) -> str:
    while True:
        code = "".join(secrets.choice(_CODE_CHARS) for _ in range(6))
        if not db.scalar(select(User.id).where(User.start_code == code)):
            return code


@router.get("/users/{user_id}/schedule", response_model=list[S.ReminderOut])
def get_schedule(user_id: str, db: Session = Depends(get_db)):
    user = _user(db, user_id)
    rows = db.scalars(select(Reminder).where(Reminder.user_id == user.id)
                      .order_by(Reminder.send_at)).all()
    return [S.ReminderOut(collection_date=r.collection_date.isoformat(),
                          send_at=r.send_at.isoformat(), bins=r.bins, status=r.status.value)
            for r in rows]


# ---- step 6: referral ---------------------------------------------------------------


@router.get("/users/{user_id}/referral", response_model=S.ReferralOut)
def get_referral(user_id: str, db: Session = Depends(get_db)):
    user = _user(db, user_id)
    count = db.scalar(select(func.count()).select_from(Referral)
                      .where(Referral.referrer_id == user.id)) or 0
    level, name, nxt = level_for(count)
    link = f"{get_settings().public_base_url}/?ref={user.referral_code}"
    return S.ReferralOut(
        code=user.referral_code, link=link, referrals=count, level=level, level_name=name,
        next_at=nxt,
        share_text=f"I use {get_settings().brand_name} for bin day reminders on WhatsApp. Sign up with your postcode: {link}",
    )


# ---- inbound WhatsApp (webhook) ----------------------------------------------------


@router.post("/inbound")
def inbound(body: S.InboundIn, db: Session = Depends(get_db)):
    """Generic inbound. The Meta webhook maps into this shape in app/api/webhooks.py."""
    return {"reply": handle_inbound(db, _e164(body.phone), body.text)}


# ---- ops ------------------------------------------------------------------------------


@router.get("/health")
def health():
    return {"ok": True, "demo_mode": get_settings().demo_mode, "coverage": coverage()}


@router.get("/ready")
def ready(db: Session = Depends(get_db)):
    """For the uptime monitor: 200 only if the site, the database and (in production) the
    scraper container all answer. 503 says which one didn't."""
    import httpx
    from sqlalchemy import text

    down = []
    try:
        db.execute(text("select 1"))
    except Exception:
        down.append("database")
    url = get_settings().scraper_url
    if url:
        try:
            httpx.get(url.rstrip("/") + "/health", timeout=5).raise_for_status()
        except Exception:
            down.append("scraper")
    if down:
        raise HTTPException(503, f"down: {', '.join(down)}")
    return {"ok": True}
