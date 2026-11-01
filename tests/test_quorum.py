"""Quorum: comparing two addresses' calendars. Pure, no database."""
from datetime import date, timedelta

from app.services.quorum import compare, signature

TUE = date(2026, 11, 3)


def weeks(start, n, step=1):
    return [start + timedelta(weeks=step * i) for i in range(n)]


def test_same_dates_agree():
    a = signature([("general", d) for d in weeks(TUE, 4)])
    assert compare(a, dict(a)) == ("agree", [])


def test_shorter_horizon_on_one_side_still_agrees():
    """One page lists 4 weeks, another 12: compare only what both cover."""
    a = signature([("general", d) for d in weeks(TUE, 4)])
    b = signature([("general", d) for d in weeks(TUE, 12)])
    assert compare(a, b)[0] == "agree"


def test_different_day_differs_and_says_where():
    a = signature([("general", d) for d in weeks(TUE, 4)])
    b = signature([("general", d + timedelta(days=1)) for d in weeks(TUE, 4)])
    verdict, diff = compare(a, b)
    assert verdict == "differ"
    assert diff[0]["category"] == "general" and diff[0]["only_ours"][0] == TUE.isoformat()


def test_day_and_month_swapped_by_a_scraper_differs():
    """3 Nov read as 11 Mar: the classic parse bug a single scrape can't catch."""
    a = signature([("recycling", date(2026, 11, 3)), ("recycling", date(2026, 11, 17))])
    b = signature([("recycling", date(2026, 11, 3)), ("recycling", date(2026, 11, 17)),
                   ("general", date(2026, 11, 10))])
    swapped = signature([("recycling", date(2026, 3, 11)), ("recycling", date(2026, 11, 17))])
    assert compare(a, b)[0] == "agree"
    assert compare(swapped, b)[0] == "differ"


def test_stream_only_one_side_has_is_not_a_disagreement():
    """Garden waste is a subscription: a neighbour may have it, this home may not."""
    a = signature([("general", d) for d in weeks(TUE, 4)])
    b = signature([("general", d) for d in weeks(TUE, 4)] + [("garden", TUE)])
    assert compare(a, b)[0] == "agree"


def test_nothing_shared_is_inconclusive():
    a = signature([("general", TUE)])
    b = signature([("recycling", TUE)])
    assert compare(a, b)[0] == "inconclusive"
    assert compare({}, b)[0] == "inconclusive"
