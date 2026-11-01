"""
Split: a disputed postcode round may really be two rounds.

verify.py flags a round when neighbours contradict it. That's either a bad scrape or a
postcode with two calendars (odd and even sides of a main road, two ends of a long street).
To tell which, ask a few more homes chosen to test both (both parities, both ends), group
them by calendar (quorum.py), and look for a rule that separates the groups (sides.py).

Only splits on evidence: exactly two calendars, at least MIN_PER_SIDE homes agreeing on
each, and a rule that explains every home asked. Anything else is logged for ops:
  one_calendar     everyone agrees now: the dispute was a passing bad scrape
  anchor_outvoted  the round's own calendar is the odd one out: its scrape is suspect
  many_calendars   more than two calendars: needs a human
  not_enough       too few homes answered to be sure
  no_rule          two calendars, but neither parity nor one cut along the street explains them

Runs from the monitor, not the loader: probing costs up to MAX_PROBE council lookups.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BinCategory,
    ChangeLog,
    CouncilStatus,
    Property,
    RoundSplit,
    User,
    UserBinSetting,
    UserStatus,
)
from app.providers.messaging import send_message
from app.services import geo, quorum, sides
from app.services import schedule as sched
from app.services import verify
from app.services.reminders import rebuild_reminders

log = logging.getLogger(__name__)
MAX_PROBE = 4
MIN_PER_SIDE = 2
LOOK_BACK = timedelta(days=14)
MOVED_MESSAGE = ("Your side of the street has different collection days from the rest of "
                 "your postcode. I've moved your reminders to your side's days.")


# Votes against a round: from neighbours' scrapes (verify.py) and from residents ("not my
# days" when the council agrees their home differs; a WRONG reply to a reminder).
VOTES = ("disputed", "neighbour_differs", "resident_differs", "wrong_day")


def disputed_rounds(db: Session) -> list[str]:
    """Postcode rounds with a recent vote against them that haven't been probed since."""
    since = datetime.now(timezone.utc) - LOOK_BACK
    rows = db.execute(select(ChangeLog.scope_id, ChangeLog.kind, ChangeLog.detected_at)
                      .where(ChangeLog.scope == "round", ChangeLog.detected_at >= since,
                             ChangeLog.kind.in_((*VOTES, "split_checked")))
                      .order_by(ChangeLog.detected_at)).all()
    latest: dict[str, str] = {}
    for key, kind, _ in rows:
        latest[key] = kind
    split = set(db.scalars(select(RoundSplit.round_key)).all())
    # A home's own round ("p:...") is one home: there's nothing to split.
    return [k for k, kind in latest.items()
            if kind != "split_checked" and k not in split and k.startswith("pc:")]


def own_calendar_vote(db: Session, left: str, prop: Property) -> None:
    """
    A resident said "these aren't my days" and got their own calendar. If the council gives
    their home different dates from the round they left, that's a vote against the round
    (maybe it's two rounds). If the council gives the same dates, the resident is telling us
    the council's own data is wrong: no probe can fix that, so it's for ops.
    """
    verdict, differences = quorum.compare(verify._round_signature(db, left),
                                          verify._round_signature(db, prop.round_key))
    if verdict == "inconclusive":
        return
    db.add(ChangeLog(scope="round", scope_id=left,
                     kind="resident_differs" if verdict == "differ" else "resident_reported",
                     diff={"from": prop.house_number or prop.uprn, "differences": differences}))


def _probe_picks(numbers: list[int]) -> list[int]:
    """Both ends of each side first, so parity and a cut along the street both get tested."""
    odd = sorted(n for n in numbers if n % 2)
    even = sorted(n for n in numbers if not n % 2)
    ends = [s[i] for i in (0, -1) for s in (odd, even) if s]
    return list(dict.fromkeys([*ends, *sorted(numbers)]))[:MAX_PROBE]


def _cluster(obs: list[tuple[int, quorum.Signature]]) -> list[tuple[quorum.Signature, list[int]]]:
    """Group homes by calendar. A home that can't be compared with anyone is left out."""
    groups: list[tuple[quorum.Signature, list[int]]] = []
    for n, sig in obs:
        verdicts = [quorum.compare(g[0], sig)[0] for g in groups]
        if "agree" in verdicts:
            groups[verdicts.index("agree")][1].append(n)
        elif not groups or "differ" in verdicts:
            groups.append((sig, [n]))
    return groups


def probe(db: Session, round_key: str) -> dict:
    anchor = sched.representative_for(db, round_key)
    council = anchor.council if anchor else None
    if not council or council.status == CouncilStatus.unsupported:
        return {"result": "skipped"}
    try:
        hits = geo.lookup_addresses(anchor.postcode)
    except Exception as e:
        log.warning("split: address lookup failed for %s: %s", anchor.postcode, e)
        hits = []
    members = db.scalars(select(Property).where(Property.round_key == round_key)).all()
    homes = {}
    for h in [*members, *hits]:
        n = sides.number_of(h.house_number)
        if n is not None and sides.side_key(db, round_key, h.house_number, h.uprn,
                                            anchor.postcode) == round_key:
            homes.setdefault(n, h)
    anchor_n = sides.number_of(anchor.house_number) or next(
        (sides.number_of(h.house_number) for h in hits if anchor.uprn and h.uprn == anchor.uprn), None)

    obs = [(anchor_n, verify._round_signature(db, round_key))] if anchor_n is not None else []
    for n in _probe_picks([n for n in homes if n != anchor_n]) if obs else []:
        items = verify._ask(council, anchor, homes[n])
        if items:
            obs.append((n, verify._signature_of(council, items)))
    groups = _cluster(obs)
    result = {"result": _verdict(groups, anchor_n),
              "groups": [sorted(g[1]) for g in groups]}
    if result["result"] == "split":
        rule = sides.infer_rule(groups[0][1], groups[1][1])
        result["rule"] = rule
        result["moved"] = _apply(db, round_key, rule)
    db.add(ChangeLog(scope="round", scope_id=round_key, kind="split_checked", diff=result))
    db.commit()
    return result


def _verdict(groups: list, anchor_n: int | None) -> str:
    if len(groups) == 1:
        return "one_calendar"
    if len(groups) > 2:
        return "many_calendars"
    if not groups or anchor_n is None:
        return "not_enough"
    ours, theirs = groups[0][1], groups[1][1]
    if len(ours) < MIN_PER_SIDE and len(theirs) >= MIN_PER_SIDE:
        return "anchor_outvoted"
    if len(ours) < MIN_PER_SIDE or len(theirs) < MIN_PER_SIDE:
        return "not_enough"
    return "split" if sides.infer_rule(ours, theirs) else "no_rule"


def _apply(db: Session, round_key: str, rule: dict) -> int:
    """
    Store the rule, move residents on the other side to their own round, fetch its
    calendar, and tell them. The side the calendar came from keeps the round as it is.
    """
    other_key = f"{round_key}~{rule['other']}"
    rule["sides"] = {rule["base"]: round_key, rule["other"]: other_key}
    db.add(RoundSplit(round_key=round_key, rule=rule))
    db.flush()
    moved = [p for p in db.scalars(select(Property).where(Property.round_key == round_key))
             if sides.side_key(db, round_key, p.house_number, p.uprn, p.postcode) == other_key]
    for p in moved:
        p.round_key = other_key
    db.flush()
    if not moved:
        return 0
    res = sched.refresh_round(db, sched.representative_for(db, other_key))
    if res.error:  # their side's calendar didn't load: they'd be worse off than before
        for p in moved:
            p.round_key = round_key
        db.flush()
        return 0
    for u in db.scalars(select(User).where(User.property_id.in_([p.id for p in moved]))):
        _add_new_bins(db, u)
        rebuild_reminders(db, u)
        if u.status == UserStatus.active and u.phone_e164:
            send_message(db, u.phone_e164, MOVED_MESSAGE, kind="change")
    return len(moved)


def _add_new_bins(db: Session, user: User) -> None:
    """Bins their side has that the old round didn't. Existing settings (a bin they turned
    off, a manual rule) are kept as they are."""
    have = set(db.scalars(select(UserBinSetting.bin_type_id)
                          .where(UserBinSetting.user_id == user.id)).all())
    for b in sched.schedule_for(db, user.property):
        if b["bin_type_id"] not in have:
            db.add(UserBinSetting(user_id=user.id, bin_type_id=b["bin_type_id"], label=b["label"],
                                  category=BinCategory(b["category"])))
    db.flush()
