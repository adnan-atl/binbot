"""
Stage 1: resolve a property's collection calendar and persist it.

resolve_property()  postcode (+uprn/house) -> Property + Council
refresh_round()     fetch via adapter -> Snapshot -> CollectionEvents (diffed)
schedule_for()      what the UI shows: bins with days/frequency/next date
"""
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import AdapterError, LookupInput, NoCollectionsForAddress, RawCollection
from app.adapters.registry import library_version, spec_for
from app.adapters.ukbcd import UKBCDAdapter
from app.core.config import get_settings
from app.models import (
    BinCategory,
    BinType,
    ChangeLog,
    CollectionEvent,
    Council,
    CouncilStatus,
    Property,
    Snapshot,
    now,
)
from app.services import checks as checker
from app.services import geo
from app.services.normalize import PURPOSE, categorise, colour_of, describe_rule

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ councils


def get_or_create_council(db: Session, gss_code: str, name: str) -> Council:
    c = db.scalar(select(Council).where(Council.gss_code == gss_code))
    spec = spec_for(gss_code, name)
    if c is None:
        c = Council(gss_code=gss_code, name=name, country=gss_code[0])
        db.add(c)
        _apply_spec(c, spec)
    elif spec and (c.adapter != spec.module or c.status == CouncilStatus.unsupported):
        # A library upgrade or a registry fix gave this council an adapter: start using it.
        _apply_spec(c, spec)
    db.flush()
    return c


def _apply_spec(c: Council, spec) -> None:
    c.adapter = spec.module if spec else None
    c.adapter_params = {"url": spec.url, "skip_get_url": spec.skip_get_url} if spec else {}
    c.needs_uprn = bool(spec and spec.needs_uprn)
    c.needs_postcode = bool(spec and spec.needs_postcode)
    c.needs_house_number = bool(spec and spec.needs_house_number)
    c.needs_browser = bool(spec and spec.needs_browser)
    c.status = CouncilStatus.ok if spec else CouncilStatus.unsupported
    c.failure_count, c.last_error = 0, None


def get_or_create_bin_type(db: Session, council: Council, label: str) -> BinType:
    bt = db.scalar(
        select(BinType).where(BinType.council_id == council.id, BinType.council_label == label)
    )
    cat = categorise(label, council.gss_code)
    if bt:
        if bt.category == BinCategory.other and cat != BinCategory.other:
            bt.category = cat  # we learned what this colour means for this council
        return bt
    bt = BinType(council_id=council.id, council_label=label,
                 category=cat, colour=colour_of(label))
    db.add(bt)
    db.flush()
    return bt


# ------------------------------------------------------------------ properties


def resolve_property(
    db: Session, postcode: str, uprn: str | None = None, house_number: str | None = None,
    address_line: str | None = None,
) -> Property:
    info = geo.lookup_postcode(postcode)
    council = get_or_create_council(db, info.gss_code, info.district)
    prop = None
    if uprn:
        prop = db.scalar(select(Property).where(Property.uprn == uprn))
    if prop is None and house_number:
        prop = db.scalar(
            select(Property).where(Property.postcode == info.postcode,
                                   Property.house_number == house_number, Property.uprn.is_(None))
        )
    if prop is None:
        prop = Property(uprn=uprn, postcode=info.postcode, house_number=house_number,
                        address_line=address_line, council_id=council.id,
                        lat=info.lat, lng=info.lng,
                        round_key=postcode_round_key(council, info.postcode))
        db.add(prop)
        db.flush()
    return prop


def postcode_round_key(council: Council, postcode: str) -> str:
    """
    Default round: everyone on a postcode shares one calendar. A UK postcode is one
    side of one street (15-40 homes) and the lorry drives streets, not houses, so the
    first neighbour's lookup serves the whole postcode. Paid address lookups happen once
    per postcode, not once per user. A home whose dates differ gets its own round via
    own_round().
    """
    return f"pc:{council.gss_code}:{postcode}"


def own_round(db: Session, prop: Property) -> None:
    """Detach a property from its postcode round (its dates differ from the neighbours')."""
    prop.round_key = f"p:{prop.id}"
    db.flush()


def round_has_calendar(db: Session, round_key: str) -> bool:
    return db.scalar(
        select(CollectionEvent.id).where(CollectionEvent.round_key == round_key,
                                         CollectionEvent.date >= date.today()).limit(1)
    ) is not None


def representative_for(db: Session, round_key: str) -> Property | None:
    """The property to fetch a round with: prefer one with a UPRN, then one with a house
    number (some councils look up by number + postcode), then the oldest."""
    return db.scalar(
        select(Property).where(Property.round_key == round_key)
        .order_by(Property.uprn.is_(None), Property.house_number.is_(None), Property.created_at)
    )


# ------------------------------------------------------------------ fetching


def _adapter_for(council: Council):
    if not council.adapter:
        return None
    spec = spec_for(council.gss_code, council.name)
    return UKBCDAdapter(spec) if spec else None


def fetch_collections(council: Council, prop: Property) -> list[RawCollection]:
    s = get_settings()
    if s.demo_mode:
        from app.services.demo import demo_collections

        return demo_collections()
    adapter = _adapter_for(council)
    if adapter is None:
        raise AdapterError("no adapter for council", retryable=False)
    return adapter.fetch(LookupInput(postcode=prop.postcode, uprn=prop.uprn,
                                     house_number=prop.house_number))


def _hash(items: list[RawCollection]) -> str:
    payload = sorted((i.label, i.date.isoformat()) for i in items)
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


@dataclass
class RefreshResult:
    snapshot: Snapshot | None
    changed: bool
    diff: dict
    error: str | None = None


def refresh_round(db: Session, prop: Property, source: str = "council") -> RefreshResult:
    """Fetch the calendar for a property's round, store it, and diff against what we had."""
    council = prop.council
    s = get_settings()
    today = date.today()
    try:
        items = fetch_collections(council, prop)
    except AdapterError as e:
        snap = Snapshot(round_key=prop.round_key, council_id=council.id, source=source,
                        adapter_version=library_version(), content_hash="", raw={},
                        ok=False, error=str(e))
        db.add(snap)
        if not isinstance(e, NoCollectionsForAddress):
            _mark_failure(db, council, str(e), retryable=e.retryable)
        db.commit()
        return RefreshResult(snapshot=snap, changed=False, diff={}, error=str(e))

    # Keep a bounded horizon; drop past dates.
    items = [i for i in items if today <= i.date <= today + timedelta(days=s.horizon_days)]
    content_hash = _hash(items)
    last = db.scalar(
        select(Snapshot).where(Snapshot.round_key == prop.round_key, Snapshot.ok.is_(True))
        .order_by(Snapshot.fetched_at.desc())
    )
    snap = Snapshot(round_key=prop.round_key, council_id=council.id, source=source,
                    adapter_version=library_version(), content_hash=content_hash,
                    raw={"bins": [{"type": i.label, "collectionDate": i.date.isoformat()}
                                  for i in items]})
    db.add(snap)
    db.flush()
    _mark_success(council)

    if last and last.content_hash == content_hash:
        db.commit()
        return RefreshResult(snapshot=snap, changed=False, diff={})

    diff = _apply_events(db, prop.round_key, council, items, snap)
    if last is not None and any(diff.values()):
        db.add(ChangeLog(scope="round", scope_id=prop.round_key, kind="dates_changed", diff=diff))
    db.commit()
    return RefreshResult(snapshot=snap, changed=last is not None, diff=diff)


def _apply_events(db: Session, round_key: str, council: Council, items: list[RawCollection],
                  snap: Snapshot) -> dict:
    """Upsert future events for the round; return {added, removed, moved}."""
    today = date.today()
    existing = db.scalars(
        select(CollectionEvent).where(CollectionEvent.round_key == round_key,
                                      CollectionEvent.date >= today)
    ).all()
    # Events filled from a neighbour by the verify step aren't this address's to diff: a
    # fetch that doesn't list them isn't a removal. If this fetch does list one, adopt it.
    filled = set(db.scalars(select(Snapshot.id).where(Snapshot.round_key == round_key,
                                                      Snapshot.source == "neighbours")).all())
    borrowed = {(e.bin_type_id, e.date): e for e in existing if e.snapshot_id in filled}
    have = {(e.bin_type_id, e.date): e for e in existing if e.snapshot_id not in filled}
    want: dict[tuple[str, date], RawCollection] = {}
    for i in items:
        bt = get_or_create_bin_type(db, council, i.label)
        want[(bt.id, i.date)] = i
    for key in want.keys() & borrowed.keys():
        borrowed[key].snapshot_id = snap.id
        have[key] = borrowed[key]

    added, removed, moved = [], [], []
    for key in want.keys() - have.keys():
        bt_id, d = key
        ev = CollectionEvent(round_key=round_key, bin_type_id=bt_id, date=d, snapshot_id=snap.id)
        # Detect a move: same bin, a date within +-7 days that disappeared.
        gone = [k for k in have.keys() - want.keys() if k[0] == bt_id and abs((k[1] - d).days) <= 7]
        if gone:
            ev.moved_from = gone[0][1]
            moved.append({"bin_type_id": bt_id, "from": gone[0][1].isoformat(), "to": d.isoformat()})
        else:
            added.append({"bin_type_id": bt_id, "date": d.isoformat()})
        db.add(ev)
    for key in have.keys() - want.keys():
        if not any(m["from"] == key[1].isoformat() and m["bin_type_id"] == key[0] for m in moved):
            removed.append({"bin_type_id": key[0], "date": key[1].isoformat()})
        db.delete(have[key])
    db.flush()
    return {"added": added, "removed": removed, "moved": moved}


def _mark_failure(db: Session, council: Council, error: str, retryable: bool):
    council.failure_count += 1
    council.last_error = error
    council.last_checked_at = now()
    if not retryable or council.failure_count >= 3:
        if council.status != CouncilStatus.broken:
            db.add(ChangeLog(scope="council", scope_id=council.id, kind="adapter_broken",
                             diff={"error": error}))
        council.status = CouncilStatus.broken
    elif council.status == CouncilStatus.ok:
        council.status = CouncilStatus.degraded


def _mark_success(council: Council):
    if council.status in (CouncilStatus.broken, CouncilStatus.degraded):
        council.status = CouncilStatus.ok
    council.failure_count = 0
    council.last_error = None
    council.last_checked_at = now()


# ------------------------------------------------------------------ read model


def schedule_for(db: Session, prop: Property, with_checks: bool = False):
    """
    Bins for a round with a derived rule each. This is what step 3 shows. Frequencies go
    through the checker (checks.py), which only claims one it can back up. With
    with_checks=True returns (bins, checks).
    """
    today = date.today()
    # Include recent past dates: councils that only publish "your next collection" reveal
    # their frequency over a few weekly refreshes, and past events are kept for that.
    events = db.scalars(
        select(CollectionEvent).where(CollectionEvent.round_key == prop.round_key,
                                      CollectionEvent.date >= today - timedelta(days=70))
        .order_by(CollectionEvent.date)
    ).all()
    by_bin: dict[str, list[CollectionEvent]] = {}
    for e in events:
        by_bin.setdefault(e.bin_type_id, []).append(e)
    out = []
    for bt_id, all_evs in by_bin.items():
        evs = [e for e in all_evs if e.date >= today]
        if not evs:
            continue
        bt = evs[0].bin_type
        rule = describe_rule([e.date for e in all_evs])
        rule["next"] = evs[0].date.isoformat()
        out.append({
            "bin_type_id": bt.id,
            "label": bt.council_label,
            "category": bt.category.value,
            "colour": bt.colour,
            "purpose": PURPOSE[bt.category],
            "days": rule["days"],
            "frequency": rule["frequency"],
            "next": rule["next"],
            "dates": [e.date.isoformat() for e in evs[:8]],
            "history": [e.date for e in all_evs],
        })
    order = ["recycling", "general", "food", "garden", "glass", "paper", "other"]
    out.sort(key=lambda b: order.index(b["category"]))
    council = prop.council
    found = checker.run(out, council.name if council else "Your council",
                        council.gss_code if council else None, today)
    return (out, found) if with_checks else out
