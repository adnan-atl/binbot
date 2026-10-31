"""Checker: frequency needs evidence, policy cross-checks the calendar, gaps are surfaced."""
from datetime import date, timedelta

from app.services import checks

TODAY = date(2026, 10, 31)
LANCASTER = "E07000121"


def _bin(cat, label, dates, days=None):
    return {"category": cat, "label": label, "bin_type_id": label, "history": dates,
            "days": days or ["X"], "frequency": "irregular"}


def test_lancaster_real_response_is_flagged_not_trusted():
    """What Lancaster returned for 92 St Leonards Gate (31 Oct 2026)."""
    food = [date(2026, 11, 3) + timedelta(weeks=i) for i in range(4)]
    domestic = [date(2026, 11, 12), date(2026, 11, 26)]
    bins = [_bin("general", "Domestic Waste", domestic), _bin("food", "Food Waste", food)]
    found = checks.run(bins, "Lancaster", LANCASTER, TODAY)

    assert bins[1]["frequency"] == "weekly" and bins[1]["frequency_source"] == "calendar"
    # Two dates 14 days apart vs a 3-weekly policy: don't claim either.
    assert bins[0]["frequency"] == "irregular"
    kinds = sorted((c["kind"], c["category"]) for c in found)
    assert kinds == [("conflict", "general"), ("missing_stream", "paper"),
                     ("missing_stream", "recycling")]
    assert "every 3 weeks" in next(c["text"] for c in found if c["kind"] == "conflict")


def test_policy_fills_frequency_when_calendar_is_too_short():
    one = [_bin("general", "Grey bin", [date(2026, 11, 12)])]
    checks.run(one, "Lancaster", LANCASTER, TODAY)
    assert one[0]["frequency"] == "three_weekly" and one[0]["frequency_source"] == "policy"


def test_three_weekly_rotation_is_recognised_from_dates():
    d0 = date(2026, 11, 2)
    bins = [_bin("general", "Grey", [d0 + timedelta(weeks=3 * i) for i in range(4)]),
            _bin("recycling", "Plastics", [d0 + timedelta(weeks=3 * i + 1) for i in range(4)]),
            _bin("paper", "Paper", [d0 + timedelta(weeks=3 * i + 2) for i in range(4)]),
            _bin("food", "Food", [d0 + timedelta(weeks=i) for i in range(10)])]
    found = checks.run(bins, "Lancaster", LANCASTER, TODAY)
    assert [b["frequency"] for b in bins] == ["three_weekly"] * 3 + ["weekly"]
    assert found == []


def test_two_dates_alone_dont_prove_a_frequency():
    b = [_bin("general", "Refuse", [date(2026, 11, 3), date(2026, 11, 17)]),
         _bin("recycling", "Recycling", [date(2026, 11, 10)])]
    checks.run(b, "Somewhere", "E00000000", TODAY)
    assert b[0]["frequency"] == "irregular"


def test_no_profile_still_flags_missing_recycling():
    b = [_bin("general", "Refuse", [date(2026, 11, 3) + timedelta(weeks=2 * i) for i in range(4)])]
    found = checks.run(b, "Somewhere", "E00000000", TODAY)
    assert b[0]["frequency"] == "fortnightly"
    assert [c["category"] for c in found] == ["recycling"]


def test_profile_not_applied_before_it_takes_effect():
    b = [_bin("general", "Grey", [date(2026, 1, 5) + timedelta(weeks=2 * i) for i in range(4)]),
         _bin("recycling", "Rec", [date(2026, 1, 12) + timedelta(weeks=2 * i) for i in range(4)])]
    assert checks.run(b, "Lancaster", LANCASTER, date(2026, 1, 1)) == []
