"""
Quorum: do two addresses on the same round tell the same story?

A postcode round assumes one lorry, one calendar. Until a second address agrees, that rests
on one scrape, and a scraper that returned someone else's calendar, swapped day and month or
mislabelled a bin looks exactly like a good answer. So a round only counts as confirmed once
another address on it matches.

Calendars are compared by category (labels vary between pages of the same council) and only
on the dates both cover (one page lists 4 weeks, another 12). A stream only one side has
(garden waste is a subscription) is a gap for the checker, not a disagreement.
"""
from collections.abc import Iterable
from datetime import date

Signature = dict[str, set[date]]


def signature(pairs: Iterable[tuple[str, date]]) -> Signature:
    """(category, date) pairs -> {category: dates}."""
    out: Signature = {}
    for cat, d in pairs:
        out.setdefault(cat, set()).add(d)
    return out


def compare(ours: Signature, theirs: Signature) -> tuple[str, list[dict]]:
    """
    "agree"         at least one shared stream, same dates on every shared stream
    "differ"        a shared stream has different dates; the list says which
    "inconclusive"  nothing to compare (no shared stream with dates in the common window)
    """
    if not ours or not theirs:
        return "inconclusive", []
    end = min(max(max(v) for v in ours.values()), max(max(v) for v in theirs.values()))
    shared, differences = 0, []
    for cat in sorted(ours.keys() & theirs.keys()):
        a = {d for d in ours[cat] if d <= end}
        b = {d for d in theirs[cat] if d <= end}
        if not a and not b:
            continue
        shared += 1
        if a != b:
            differences.append({"category": cat,
                                "only_ours": sorted(d.isoformat() for d in a - b),
                                "only_theirs": sorted(d.isoformat() for d in b - a)})
    if differences:
        return "differ", differences
    return ("agree" if shared else "inconclusive"), []
