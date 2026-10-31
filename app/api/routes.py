import re
import secrets
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.manual import expand_rule
from app.adapters.registry import coverage, spec_for
from app.api import schemas as S
from app.core.config import get_settings
from app.core.db import get_db
from app.models import (
    BinCategory,
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
from app.services import geo
from app.services.normalize import describe_rule
from app.services.reminders import handle_inbound, rebuild_reminders
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

router = APIRouter(prefix="/api")


def _user(db: Session, user_id: str) -> User:
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    return u


# ---- step 1: postcode -------------------------------------------------------------


@router.get("/postcode/{postcode}", response_model=S.PostcodeOut)
def postcode_lookup(postcode: str, addresses: bool = False, db: Session = Depends(get_db)):
    """
    What we know about a postcode. `addresses=true` runs the (paid) address lookup; the
    UI only asks for it when the council needs an address AND no neighbour has signed up
    yet (round_known=false). Otherwise the first neighbour's calendar is reused for free.
    """
    try:
        info = geo.lookup_postcode(postcode)
    except geo.BadPostcode:
        raise HTTPException(400, "That doesn't look like a UK postcode")
    spec = spec_for(info.gss_code)
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
def signup_start(body: S.StartIn, db: Session = Depends(get_db)):
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
        res = refresh_round(db, prop, source=source)
        if res.error:
            error = res.error
        else:
            source = source if source == "demo" else "council"
    if not error:
        _seed_settings(db, user, prop)
    if user.referred_by_id:
        db.add(Referral(referrer_id=user.referred_by_id, referred_id=user.id))
    db.commit()
    return S.StartOut(user_id=user.id, property_id=prop.id, council=council.name,
                      source=source if not error else "manual", error=error, shared=shared,
                      bins=_bins_out(db, user))


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
def own_calendar(user_id: str, body: S.OwnCalendarIn, db: Session = Depends(get_db)):
    """
    'These aren't my days': detach this home from the postcode round and fetch its own
    calendar. This is the one place a paid address lookup happens for a non-first neighbour.
    """
    user = _user(db, user_id)
    prop = user.property
    if prop is None:
        raise HTTPException(400, "No property on this user")
    if body.uprn:
        prop.uprn, prop.house_number, prop.address_line = body.uprn, body.house_number, body.address_line
    elif body.house_number:
        prop.house_number, prop.address_line = body.house_number, body.address_line
    own_round(db, prop)
    source = "demo" if get_settings().demo_mode else "council"
    res = refresh_round(db, prop, source=source)
    error = res.error
    if not error:
        _seed_settings(db, user, prop)
    db.commit()
    return S.StartOut(user_id=user.id, property_id=prop.id, council=prop.council.name,
                      source=source if not error else "manual", error=error, shared=False,
                      bins=_bins_out(db, user))


def _bins_out(db: Session, user: User) -> list[S.BinOut]:
    council_bins = {b["bin_type_id"]: b for b in schedule_for(db, user.property)} if user.property else {}
    out = []
    for s in db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == user.id)):
        if s.override_rule:
            dates = expand_rule(s.override_rule, date.today(), 60)
            rule = describe_rule(dates)
            rule["days"] = s.override_rule.get("days", rule["days"])
            rule["frequency"] = s.override_rule.get("frequency", rule["frequency"])
            colour, manual = None, True
        elif s.bin_type_id in council_bins:
            cb = council_bins[s.bin_type_id]
            rule = {"days": cb["days"], "frequency": cb["frequency"], "next": cb["next"],
                    "purpose": cb["purpose"]}
            colour, manual = cb["colour"], False
        else:
            rule, colour, manual = {"days": [], "frequency": "irregular", "next": None}, None, True
        out.append(S.BinOut(setting_id=s.id, bin_type_id=s.bin_type_id, label=s.label,
                            category=s.category.value, colour=colour, enabled=s.enabled,
                            manual=manual, **rule))
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
    if clash:
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
    user.status = UserStatus.active
    n = rebuild_reminders(db, user)
    db.commit()
    return {"status": "active", "scheduled": n}


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
        share_text=f"I use BinBot for bin day reminders on WhatsApp. Sign up with your postcode: {link}",
    )


# ---- inbound WhatsApp (webhook) ----------------------------------------------------


@router.post("/inbound")
def inbound(body: S.InboundIn, db: Session = Depends(get_db)):
    """Generic inbound. Twilio/Meta webhooks map into this shape in app/api/webhooks.py."""
    return {"reply": handle_inbound(db, _e164(body.phone), body.text)}


# ---- ops ------------------------------------------------------------------------------


@router.get("/health")
def health():
    return {"ok": True, "demo_mode": get_settings().demo_mode, "coverage": coverage()}
