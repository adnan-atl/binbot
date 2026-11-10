"""Generate concrete dates from a user-entered rule (unsupported councils, or overrides)."""
from datetime import date, timedelta

_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
_PERIOD = {"weekly": 7, "fortnightly": 14, "three_weekly": 21, "four_weekly": 28}


MAX_MOVE_DAYS = 10  # a holiday shifts a collection by a few days, never weeks


def expand_rule(rule: dict, start: date, horizon_days: int) -> list[date]:
    """
    The rule's dates, with the resident's holiday changes applied:
    rule["moves"] = {"2026-12-25": "2026-12-28", "2027-01-01": None}  (None: no collection).
    """
    moves = rule.get("moves") or {}
    if not moves:
        return _expand(rule, start, horizon_days)
    pad = timedelta(days=MAX_MOVE_DAYS)
    end = start + timedelta(days=horizon_days)
    # Pin the phase to `start` as before, then look a little either side: a collection that
    # usually falls just outside the window can be moved into it (31 Dec -> 2 Jan).
    pinned = rule if rule.get("anchor") else {**rule, "anchor": start.isoformat()}
    out = set()
    for d in _expand(pinned, start - pad, horizon_days + 2 * MAX_MOVE_DAYS):
        iso = d.isoformat()
        moved = moves.get(iso, iso)
        if moved:
            nd = date.fromisoformat(moved)
            if start <= nd <= end:
                out.add(nd)
    return sorted(out)


def clean_moves(raw: dict | None) -> dict:
    """Keep only sensible holiday changes: real dates, a few days apart, at most 30."""
    out: dict = {}
    for k, v in list((raw or {}).items())[:30]:
        try:
            usual = date.fromisoformat(k)
            new = date.fromisoformat(v) if v else None
        except (TypeError, ValueError):
            continue
        # new == usual is kept: "no change", it undoes the council's move for that date
        if new is None or abs((new - usual).days) <= MAX_MOVE_DAYS:
            out[usual.isoformat()] = new.isoformat() if new else None
    return out


def _expand(rule: dict, start: date, horizon_days: int) -> list[date]:
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
