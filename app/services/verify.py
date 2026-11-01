"""
Verify step: runs while the resident watches the loader, before they see their bins.

Two jobs, both answered by asking the council about one or two other homes on the same
postcode (same street, same lorry):

  confirm  the round's calendar rests on one scrape. A neighbour whose dates match confirms
           it (quorum.py); one whose dates don't is a vote against, and a second neighbour
           breaks the tie. Disagreements go to change_log for ops, never to the resident.
  fill     a stream the council's policy lists but this calendar lacks is filled from an
           agreeing neighbour (hierarchy level 5, neighbours, beats "nothing"). Filled events
           are tagged with a "neighbours" snapshot so a later refresh of this address doesn't
           count them as removed. A neighbour that disagrees on the shared streams is likely
           on another round, so nothing is filled from it.

Round status, logged on each "verified" row:
  confirmed    a neighbour agrees (2 of 2, or 2 of 3 when another neighbour differs)
  disputed     every neighbour that answered differs
  unconfirmed  nobody answered, or nothing to compare

Bounded: at most MAX_NEIGHBOURS extra council lookups per round, at most once a week. Usually
one: the second is only asked for to break a tie or fill a gap the first couldn't.
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
from app.services import geo, quorum
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


def _ask(council, prop: Property, n) -> list | None:
    """The neighbour's calendar within the horizon, or None if the council had nothing."""
    today, horizon = date.today(), get_settings().horizon_days
    stand_in = SimpleNamespace(postcode=prop.postcode, uprn=n.uprn, house_number=n.house_number)
    try:
        items = sched.fetch_collections(council, stand_in)
    except AdapterError as e:
        log.info("verify: neighbour %s failed: %s", n.house_number, e)
        return None
    return [i for i in items if today <= i.date <= today + timedelta(days=horizon)] or None


def _signature_of(council, items) -> quorum.Signature:
    return quorum.signature((categorise(i.label, council.gss_code).value, i.date) for i in items)


def _round_signature(db: Session, round_key: str) -> quorum.Signature:
    events = db.scalars(select(CollectionEvent).where(CollectionEvent.round_key == round_key,
                                                      CollectionEvent.date >= date.today())).all()
    return quorum.signature((e.bin_type.category.value, e.date) for e in events)


def _status(agreed: list, differed: list) -> str:
    return "confirmed" if agreed else "disputed" if differed else "unconfirmed"


def _log_disagreements(db: Session, round_key: str, status: str, differed: list) -> None:
    """Disputed: the round itself is in doubt. Outvoted: one neighbour is on another round,
    or its scrape is wrong. Either way ops want the raw comparison."""
    if differed:
        db.add(ChangeLog(scope="round", scope_id=round_key,
                         kind="disputed" if status == "disputed" else "neighbour_differs",
                         diff={"differed": differed}))


def _borrowed_snapshot(db: Session, prop: Property, council, n, items) -> Snapshot:
    snap = Snapshot(round_key=prop.round_key, council_id=council.id, source="neighbours",
                    adapter_version=library_version(), content_hash="",
                    raw={"from": n.house_number, "bins": [
                        {"type": i.label, "collectionDate": i.date.isoformat()} for i in items]})
    db.add(snap)
    db.flush()
    return snap


def _borrowed_from(db: Session, round_key: str) -> set[str]:
    """Homes this round already borrowed dates from. Asking them again would only agree with
    their own dates, so they can't confirm anything."""
    raws = db.scalars(select(Snapshot.raw).where(Snapshot.round_key == round_key,
                                                 Snapshot.source == "neighbours")).all()
    return {str(r.get("from")) for r in raws if r and r.get("from")}


def verify_round(db: Session, prop: Property, confirm: bool = True) -> dict:
    """
    Fill gaps and confirm this round's calendar with a neighbour.

    confirm=False is the loader's call: it only spends a council lookup when a bin is
    missing, because that changes what the resident sees. Confirming a round is for ops, so
    the API runs it after the response (confirm=True, in the background).
    """
    council = prop.council
    result = {"looked_at": 0, "filled": [], "status": "unconfirmed"}
    if not council or council.status == CouncilStatus.unsupported \
            or _recently_verified(db, prop.round_key):
        return result
    _, found = sched.schedule_for(db, prop, with_checks=True)
    gaps = {c["category"]: c for c in found if c["kind"] == "missing_stream"}
    if not confirm and not gaps:
        return result
    unconfirmed_fill, borrowed = [], _borrowed_from(db, prop.round_key)
    ours = _round_signature(db, prop.round_key)
    if not ours and not gaps:
        return result

    agreed, differed = [], []
    for n in _neighbours(prop):
        if agreed and not gaps:
            break
        if not gaps and not confirm:
            break
        if n.house_number in borrowed:
            continue
        result["looked_at"] += 1
        items = _ask(council, prop, n)
        if items is None:
            continue
        verdict, differences = quorum.compare(ours, _signature_of(council, items))
        if verdict == "differ":
            differed.append({"from": n.house_number, "differences": differences})
            continue
        if verdict == "agree":
            agreed.append(n.house_number)
        wanted = [i for i in items if categorise(i.label, council.gss_code).value in gaps]
        if not wanted:
            continue
        if verdict == "inconclusive":  # nothing shared to compare: could be another round
            unconfirmed_fill.append(n.house_number)
        snap = _borrowed_snapshot(db, prop, council, n, wanted)
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

    result["status"] = _status(agreed, differed)
    _log_disagreements(db, prop.round_key, result["status"], differed)
    # The loader's call that only filled a gap hasn't confirmed anything: log it as "filled"
    # so the background confirm still runs this week.
    kind = "verified" if confirm or result["status"] != "unconfirmed" else "filled"
    db.add(ChangeLog(scope="round", scope_id=prop.round_key, kind=kind,
                     diff={**result, "still_missing": sorted(gaps), "agreed": agreed,
                           "differed": differed, "filled_unconfirmed_from": unconfirmed_fill}))
    db.flush()
    return result


def rescue_round(db: Session, prop: Property) -> bool:
    """
    This address's lookup failed. Before giving up (and handing the resident a blank form),
    try the same council for one or two neighbours on the postcode: same street, same lorry.
    The first neighbour with a calendar becomes the round's calendar, tagged as borrowed,
    straight away: the resident is waiting. Confirming it with another home happens after
    the response (verify_round), which skips the home we borrowed from.
    """
    council = prop.council
    if not council or council.status == CouncilStatus.unsupported:
        return False
    first, agreed, differed = None, [], []
    for n in _neighbours(prop):
        items = _ask(council, prop, n)
        if items is None:
            continue
        first = (n, items)
        break
    if first is None:
        return False

    n, items = first
    snap = _borrowed_snapshot(db, prop, council, n, items)
    sched._apply_events(db, prop.round_key, council, items, snap)
    sched._mark_success(council)
    db.add(ChangeLog(scope="round", scope_id=prop.round_key, kind="rescued",
                     diff={"from": n.house_number}))
    db.flush()
    return True
