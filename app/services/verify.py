"""
Verify step: runs while the resident watches the loader, before they see their bins.

When the checker finds a gap (a stream the council's policy lists but this address's
calendar doesn't) or a disagreement, don't show it and don't ask the resident. Ask the
council about one or two other homes on the same postcode, same lorry, and fill the gap
from them (hierarchy level 5, neighbours, beats "nothing"). Filled events are tagged with a
"neighbours" snapshot so a later refresh of this address doesn't count them as removed.

Bounded: at most MAX_NEIGHBOURS extra council lookups per round, at most once a week.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import AdapterError
from app.adapters.registry import library_version
from app.core.config import get_settings
from app.models import ChangeLog, CollectionEvent, CouncilStatus, Property, Snapshot
from app.services import geo
from app.services import schedule as sched
from app.services.normalize import categorise

log = logging.getLogger(__name__)
MAX_NEIGHBOURS = 2
RECHECK_AFTER = timedelta(days=7)


def _recently_verified(db: Session, round_key: str) -> bool:
    since = datetime.now(timezone.utc) - RECHECK_AFTER
    return db.scalar(
        select(ChangeLog.id).where(ChangeLog.scope_id == round_key, ChangeLog.kind == "verified",
                                   ChangeLog.detected_at >= since).limit(1)
    ) is not None


def _neighbours(prop: Property) -> list:
    """Other homes on the postcode, spread out (first and middle), never this one."""
    try:
        hits = geo.lookup_addresses(prop.postcode)  # cached; usually already fetched at sign-up
    except Exception as e:  # address provider down: nothing to cross-check with
        log.warning("verify: address lookup failed for %s: %s", prop.postcode, e)
        return []
    others, seen = [], {prop.house_number}
    for h in hits:
        if not h.house_number or h.house_number in seen or (prop.uprn and h.uprn == prop.uprn):
            continue
        seen.add(h.house_number)
        others.append(h)
    picks = [others[0], others[len(others) // 2]] if len(others) > 1 else others
    return list({id(p): p for p in picks}.values())[:MAX_NEIGHBOURS]


def verify_round(db: Session, prop: Property) -> dict:
    """Resolve checker findings for this round before the resident sees it."""
    council = prop.council
    _, found = sched.schedule_for(db, prop, with_checks=True)
    gaps = {c["category"]: c for c in found if c["kind"] == "missing_stream"}
    result = {"looked_at": 0, "filled": []}
    if not gaps or not council or council.status == CouncilStatus.unsupported \
            or _recently_verified(db, prop.round_key):
        return result

    today, horizon = date.today(), get_settings().horizon_days
    for n in _neighbours(prop):
        if not gaps:
            break
        result["looked_at"] += 1
        stand_in = SimpleNamespace(postcode=prop.postcode, uprn=n.uprn, house_number=n.house_number)
        try:
            items = sched.fetch_collections(council, stand_in)
        except AdapterError as e:
            log.info("verify: neighbour %s failed: %s", n.house_number, e)
            continue
        wanted = [i for i in items if categorise(i.label, council.gss_code).value in gaps
                  and today <= i.date <= today + timedelta(days=horizon)]
        if not wanted:
            continue
        snap = Snapshot(round_key=prop.round_key, council_id=council.id, source="neighbours",
                        adapter_version=library_version(), content_hash="",
                        raw={"from": n.house_number, "bins": [
                            {"type": i.label, "collectionDate": i.date.isoformat()} for i in wanted]})
        db.add(snap)
        db.flush()
        for i in wanted:
            bt = sched.get_or_create_bin_type(db, council, i.label)
            exists = db.scalar(select(CollectionEvent.id).where(
                CollectionEvent.round_key == prop.round_key, CollectionEvent.bin_type_id == bt.id,
                CollectionEvent.date == i.date))
            if not exists:
                db.add(CollectionEvent(round_key=prop.round_key, bin_type_id=bt.id, date=i.date,
                                       snapshot_id=snap.id))
            cat = bt.category.value
            if cat in gaps:
                gaps.pop(cat)
                result["filled"].append(cat)
        db.flush()

    db.add(ChangeLog(scope="round", scope_id=prop.round_key, kind="verified",
                     diff={**result, "still_missing": sorted(gaps)}))
    db.flush()
    return result
