"""Every-2/3/4-weeks bins: the week the resident picks (its Monday, sent as the anchor) is 'on'."""
from datetime import date

from app.adapters.manual import expand_rule


def test_fortnightly_single_day_starts_on_the_picked_week():
    rule = {"days": ["THU"], "frequency": "fortnightly", "anchor": "2026-11-09"}  # week of Thu 12
    assert expand_rule(rule, date(2026, 11, 4), 40)[:3] == [date(2026, 11, 12), date(2026, 11, 26), date(2026, 12, 10)]


def test_fortnightly_twice_a_week_keeps_both_days_in_the_same_week():
    rule = {"days": ["TUE", "FRI"], "frequency": "fortnightly", "anchor": "2026-11-09"}
    assert expand_rule(rule, date(2026, 11, 4), 20) == [date(2026, 11, 10), date(2026, 11, 13), date(2026, 11, 24)]


def test_three_weekly_third_week():
    rule = {"days": ["TUE"], "frequency": "three_weekly", "anchor": "2026-11-23"}
    assert expand_rule(rule, date(2026, 11, 4), 50) == [date(2026, 11, 23 + 1), date(2026, 12, 15)]
