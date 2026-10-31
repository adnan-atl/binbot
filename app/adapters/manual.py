"""Generate concrete dates from a user-entered rule (unsupported councils, or overrides)."""
from datetime import date, timedelta

_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
_PERIOD = {"weekly": 7, "fortnightly": 14, "three_weekly": 21, "four_weekly": 28}


def expand_rule(rule: dict, start: date, horizon_days: int) -> list[date]:
    """
    rule = {"days": ["TUE", "FRI"], "frequency": "weekly", "anchor": "2026-11-03"}
    `anchor` is a known collection date; it fixes the phase for fortnightly / 4-weekly rules.
    Twice-weekly = two days with frequency weekly.
    """
    days = [_WEEKDAYS[d] for d in rule.get("days", []) if d in _WEEKDAYS]
    if not days:
        return []
    period = _PERIOD.get(rule.get("frequency", "weekly"), 7)
    anchor = date.fromisoformat(rule["anchor"]) if rule.get("anchor") else start
    end = start + timedelta(days=horizon_days)
    out: list[date] = []
    for wd in days:
        # First occurrence of this weekday on or after the anchor's week.
        first = anchor + timedelta(days=(wd - anchor.weekday()) % 7)
        # Walk back so we cover dates between `start` and the anchor too.
        while first - timedelta(days=period) >= start:
            first -= timedelta(days=period)
        d = first
        while d <= end:
            if d >= start:
                out.append(d)
            d += timedelta(days=period)
    return sorted(set(out))
