"""
Sides: which part of a split postcode a home is on.

Most postcodes are one round. Some are two: odd and even sides of a main road collected on
different days, or the two ends of a long street. split.py finds those and stores a rule
(RoundSplit); this module reads it. Pure apart from the RoundSplit lookup, so schedule.py
can use it without importing split.py.
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RoundSplit
from app.services import geo

_NUM = re.compile(r"\d+")


def number_of(house_number: str | None) -> int | None:
    """'12' -> 12, '12A' -> 12, 'Flat 3, 18' -> 18 (the building, not the flat), 'Rose
    Cottage' -> None."""
    nums = _NUM.findall(house_number or "")
    return int(nums[-1]) if nums else None


def infer_rule(ours: list[int], theirs: list[int]) -> dict | None:
    """
    House numbers on two calendars -> the rule that tells them apart, or None. `ours` is the
    side the round's calendar came from. Parity first (the common case: a main road), then
    one clean cut along the street. The cut sits halfway between the two groups, so a home
    in the gap is a guess; "not my days" still covers it.
    """
    if all(n % 2 for n in ours) and not any(n % 2 for n in theirs):
        return {"by": "parity", "base": "odd", "other": "even"}
    if not any(n % 2 for n in ours) and all(n % 2 for n in theirs):
        return {"by": "parity", "base": "even", "other": "odd"}
    if max(ours) < min(theirs):
        return {"by": "threshold", "at": (max(ours) + min(theirs)) / 2, "base": "lo", "other": "hi"}
    if max(theirs) < min(ours):
        return {"by": "threshold", "at": (max(theirs) + min(ours)) / 2, "base": "hi", "other": "lo"}
    return None


def side_of(rule: dict, n: int | None) -> str | None:
    if n is None:
        return None
    if rule["by"] == "parity":
        return "odd" if n % 2 else "even"
    return "lo" if n < rule["at"] else "hi"


def number_for(house_number: str | None, uprn: str | None, postcode: str) -> int | None:
    """A home's street number; signed up by UPRN alone, the cached address list knows it."""
    n = number_of(house_number)
    if n is None and uprn:
        try:
            n = number_of(next((h.house_number for h in geo.lookup_addresses(postcode)
                                if h.uprn == uprn), None))
        except Exception:
            n = None
    return n


def side_key(db: Session, base_key: str, house_number: str | None, uprn: str | None,
             postcode: str) -> str | None:
    """
    The round a home on `base_key` belongs to: base_key when the postcode isn't split, the
    side's key when it is (following splits of splits), None when it's split and we can't
    tell the home's number.
    """
    key = base_key
    for _ in range(4):  # a side can itself be split; never loop forever on bad data
        split = db.scalar(select(RoundSplit).where(RoundSplit.round_key == key))
        if split is None:
            return key
        side = side_of(split.rule, number_for(house_number, uprn, postcode))
        if side is None:
            return None
        key = split.rule["sides"][side]
    return key
