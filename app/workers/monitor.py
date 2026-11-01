"""
Stage 2: the monitor agent.

Four jobs, run by the worker process (see app/workers/main.py):
  health_check()   daily   one probe per council adapter; flips status, logs breakage
  refresh_stale()  hourly  re-fetch rounds older than REFRESH_INTERVAL_DAYS (tighter
                           before bank holidays); diff; rebuild reminders; tell users
  dispatch()       minute  send reminders that are due
  resolve_disputes() daily probe rounds neighbours contradicted; split the ones that are
                           really two rounds (split.py)
"""
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    BinType,
    ChangeLog,
    Council,
    CouncilStatus,
    Property,
    Snapshot,
    User,
    UserStatus,
)
from app.providers.messaging import send_message
from app.services import split
from app.services.reminders import dispatch_due, rebuild_reminders
from app.services.verify import verify_round
from app.services.schedule import refresh_round, representative_for

log = logging.getLogger(__name__)

# England & Wales bank holidays that move collections. Extend from gov.uk/bank-holidays.
BANK_HOLIDAYS = [
    date(2026, 12, 25), date(2026, 12, 28), date(2027, 1, 1), date(2027, 4, 2), date(2027, 4, 5),
    date(2027, 5, 3), date(2027, 5, 31), date(2027, 8, 30), date(2027, 12, 27), date(2027, 12, 28),
]


def near_bank_holiday(today: date, window_days: int) -> bool:
    return any(0 <= (h - today).days <= window_days for h in BANK_HOLIDAYS)


def refresh_stale(db: Session, today: date | None = None) -> dict:
    """Re-fetch rounds whose last good snapshot is older than the interval."""
    s = get_settings()
    today = today or date.today()
    interval = timedelta(days=s.refresh_interval_days)
    if near_bank_holiday(today, s.refresh_window_days_before_holiday):
        interval = timedelta(days=2)  # councils publish holiday changes late; poll harder
    cutoff = datetime.now(timezone.utc) - interval

    last_ok = (
        select(Snapshot.round_key, func.max(Snapshot.fetched_at).label("at"))
        .where(Snapshot.ok.is_(True)).group_by(Snapshot.round_key).subquery()
    )
    stale_keys = set(db.scalars(select(last_ok.c.round_key).where(last_ok.c.at < cutoff)).all())
    # Rounds with users but no good snapshot at all.
    never = set(db.scalars(
        select(Property.round_key).join(User, User.property_id == Property.id)
        .where(User.status == UserStatus.active)
    ).all()) - set(db.scalars(select(last_ok.c.round_key)).all())

    stats = {"checked": 0, "changed": 0, "failed": 0, "notified": 0}
    for key in stale_keys | never:
        prop = representative_for(db, key)
        if not prop or not prop.council or prop.council.status == CouncilStatus.unsupported:
            continue
        res = refresh_round(db, prop)
        stats["checked"] += 1
        if res.error:
            stats["failed"] += 1
            continue
        verify_round(db, prop)  # fill gaps from neighbours again (at most weekly)
        db.commit()
        if res.changed:
            stats["changed"] += 1
            stats["notified"] += _propagate_change(db, prop, res.diff)
    db.commit()
    return stats


def _propagate_change(db: Session, prop: Property, diff: dict) -> int:
    """Rebuild reminders for everyone on the round and tell them what moved."""
    users = db.scalars(
        select(User).join(Property, User.property_id == Property.id)
        .where(Property.round_key == prop.round_key, User.status == UserStatus.active)
    ).all()
    moved = diff.get("moved", [])
    notified = 0
    for u in users:
        rebuild_reminders(db, u)
        if moved and u.phone_e164:
            lines = []
            for m in moved:
                bt = db.get(BinType, m["bin_type_id"])
                frm = date.fromisoformat(m["from"]).strftime("%a %-d %b")
                to = date.fromisoformat(m["to"]).strftime("%a %-d %b")
                lines.append(f"{bt.council_label if bt else 'A bin'}: {frm} -> {to}")
            body = "Heads up, your council moved a collection:\n" + "\n".join(lines) + \
                   "\nI've updated your reminders."
            send_message(db, u.phone_e164, body, kind="change")
            notified += 1
    log_row = db.scalar(
        select(ChangeLog).where(ChangeLog.scope_id == prop.round_key,
                                ChangeLog.kind == "dates_changed")
        .order_by(ChangeLog.detected_at.desc())
    )
    if log_row:
        log_row.users_notified = notified
    return notified


def health_check(db: Session) -> dict:
    """
    Probe each council adapter with one real property we already serve. A council with
    no users isn't probed: nothing to break yet, and it keeps us polite to their servers.
    """
    councils = db.scalars(
        select(Council).where(Council.adapter.is_not(None),
                              Council.status != CouncilStatus.unsupported)
    ).all()
    out = {"ok": 0, "broken": 0, "skipped": 0}
    for c in councils:
        prop = db.scalar(
            select(Property).join(User, User.property_id == Property.id)
            .where(Property.council_id == c.id, User.status == UserStatus.active)
            .order_by(Property.uprn.is_(None))  # prefer a home we can actually look up
        )
        if not prop:
            out["skipped"] += 1
            continue
        was = c.status
        res = refresh_round(db, prop)
        if res.error:
            out["broken"] += 1
        else:
            out["ok"] += 1
            if was == CouncilStatus.broken:
                db.add(ChangeLog(scope="council", scope_id=c.id, kind="adapter_recovered", diff={}))
    db.commit()
    return out


def resolve_disputes(db: Session) -> dict:
    out: dict[str, int] = {}
    for key in split.disputed_rounds(db):
        try:
            result = split.probe(db, key)["result"]
        except Exception as e:  # one broken round mustn't stop the rest, tonight or any night
            log.exception("resolve_disputes: probe failed for %s", key)
            db.rollback()
            result = "error"
            # Marks it probed, so it waits for a fresh vote instead of failing first every night.
            db.add(ChangeLog(scope="round", scope_id=key, kind="split_checked",
                             diff={"result": "error", "error": f"{type(e).__name__}: {e}"[:300]}))
            db.commit()
        out[result] = out.get(result, 0) + 1
    return out


def dispatch(db: Session) -> int:
    return dispatch_due(db)
