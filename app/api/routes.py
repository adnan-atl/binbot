import logging
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.manual import clean_moves
from app.adapters.registry import coverage, spec_for
from app.api import schemas as S
from app.core.config import get_settings
from app.core.db import get_db
from app.core.guard import client_ip, human_check_on, issue_pass, rate_limit, require_human, verify_turnstile
from app.models import (
    BinCategory,
    Referral,
    Reminder,
    ReminderMode,
    User,
    UserBinSetting,
    UserStatus,
    level_for,
    now,
)
from app.services import analytics, geo, signup, split
from app.services.reminders import handle_inbound, rebuild_reminders, send_welcome, start_link
from app.services.schedule import (
    get_or_create_council,
    own_round,
    postcode_round_key,
    refresh_round,
    round_has_calendar,
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
    What we know about a postcode. `addresses=true` runs the (paid) address lookup, which the
    site does for every sign-up so each resident confirms their exact home. round_known says a
    neighbour's calendar can be reused, so sign-up skips the council lookup.
    """
    rate_limit(request, "postcode", 60, 600)
    if addresses:
        require_human(request)
        rate_limit(request, "addresses", 20, 3600)  # every sign-up confirms its address
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
        res = signup.start_signup(db, body.postcode, body.uprn, body.house_number,
                                  body.address_line, referral_code=body.referral_code)
    except geo.BadPostcode:
        raise HTTPException(400, "That doesn't look like a UK postcode")
    user = res.user
    bins = _bins_out(db, user)
    analytics.track(user, "signup_started", via="website", calendar=res.source,
                    shared=bool(res.shared), calendar_error=bool(res.error), bins=len(bins))
    return S.StartOut(user_id=user.id, property_id=res.prop.id, council=res.prop.council.name,
                      source=res.source, error=res.error, shared=res.shared, bins=bins)


# Shared with the WhatsApp sign-up: see services/signup.py
_log_checks = signup.log_checks
_seed_settings = signup.seed_settings
_bins_out = signup.bins_out
_first_showable_day = signup.first_showable_day
SAME_DAY_CUTOFF = signup.SAME_DAY_CUTOFF


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
    signup.fill_gaps(db, user)
    db.commit()
    out = _bins_out(db, user)
    if user.property:
        background.add_task(signup.confirm_round, user.property.id)
    return out


# ---- step 3: confirm / edit bins ---------------------------------------------------

BIN_COLOURS = {"blue", "black", "green", "brown", "grey", "purple", "red", "yellow", "white", "orange", "pink"}


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
        if b.colour is not None:
            c = b.colour.strip().lower()
            if c and c not in BIN_COLOURS:
                raise HTTPException(400, "Unknown bin colour")
            s.colour = c or None
        if b.days is not None:
            moves = clean_moves(b.moves) if b.moves is not None else \
                (s.override_rule or {}).get("moves", {})
            s.override_rule = {"days": b.days, "frequency": b.frequency or "weekly",
                               "anchor": b.anchor, "moves": moves}
        elif b.moves is not None and s.override_rule:
            s.override_rule = {**s.override_rule, "moves": clean_moves(b.moves)}
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
    if body.mode != ReminderMode.night_before.value:  # reminders only come the evening before
        raise HTTPException(400, "mode must be night_before")
    user.reminder_mode = ReminderMode.night_before
    if not re.fullmatch(r"\d{2}:\d{2}", body.time):
        raise HTTPException(400, "time must be HH:MM")
    user.reminder_time = body.time
    was_active = user.status == UserStatus.active
    if not was_active and get_settings().meta_display_number:
        # Don't message a typed number cold: the resident says hi to us from their WhatsApp
        # first (handle_inbound), which proves the number and starts their reminders.
        if not user.start_code:
            user.start_code = _new_start_code(db)  # still keys the QR code and older links
        if user.hi_prompted_at is None:  # the sign-up nudge counts from here
            user.hi_prompted_at = now()
        db.commit()
        return {"status": "awaiting_hi", "scheduled": 0, "start_code": user.start_code,
                "start_link": start_link(user.start_code)}
    if was_active:
        n = rebuild_reminders(db, user)
    else:
        n = signup.activate(db, user)
        send_welcome(db, user.phone_e164, user)
    db.commit()
    return {"status": "active", "scheduled": n}


_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


def _new_start_code(db: Session) -> str:
    while True:
        code = "".join(secrets.choice(_CODE_CHARS) for _ in range(6))
        if not db.scalar(select(User.id).where(User.start_code == code)):
            return code


@router.get("/users/{user_id}/status")
def get_status(user_id: str, db: Session = Depends(get_db)):
    """Whether reminders are on yet: the say-hi page on a computer waits on this while the
    resident sends their hi from their phone."""
    return {"status": _user(db, user_id).status.value}


@router.get("/users/{user_id}/start-qr.svg", include_in_schema=False)
def get_start_qr(user_id: str, db: Session = Depends(get_db)):
    """QR code of the say-hi link, for signing up on a computer: scan it with the phone that
    has WhatsApp and the message is ready to send."""
    import io

    import segno

    user = _user(db, user_id)
    if not user.start_code:
        raise HTTPException(404, "nothing to scan")
    buf = io.BytesIO()
    segno.make(start_link(user.start_code), error="m").save(buf, kind="svg", scale=8, border=2,
                                                          dark="#1A1A1A", xmldecl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@router.get("/preview-qr.svg", include_in_schema=False)
def get_preview_qr():
    """Sample QR for previewing the say-hi step (/?preview=hi): a link to the site, no account."""
    import io

    import segno

    buf = io.BytesIO()
    segno.make(get_settings().public_base_url, error="m").save(buf, kind="svg", scale=8, border=2,
                                                              dark="#1A1A1A", xmldecl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml")


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
    from app.services.reminders import share_link, share_message

    return S.ReferralOut(
        code=user.referral_code, link=share_link(user), referrals=count, level=level,
        level_name=name, next_at=nxt, share_text=share_message(user),
    )


# ---- inbound WhatsApp (webhook) ----------------------------------------------------


@router.post("/inbound")
def inbound(body: S.InboundIn, db: Session = Depends(get_db)):
    """Generic inbound. The Meta webhook maps into this shape in app/api/webhooks.py."""
    phone = _e164(body.phone)
    reply = handle_inbound(db, phone, body.text)
    from app.services.inbox import note_inbound

    note_inbound(db, phone)
    return {"reply": reply}


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
