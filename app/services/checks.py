"""
Checker: is what we're about to show someone actually right?

Sources, most trusted first. A higher source wins for display; a disagreement between two
sources is never resolved silently, it becomes a check the user sees and ops can review.

  1. The resident, for their own home   (they see the lorry; override_rule, handled upstream)
  2. Council calendar for this address   (scraped dates; specific and current)
  3. Council's stated rule on that page  ("emptied every 14 days"; Glasgow)
  4. Council service policy              (profiles.py: "3-weekly from 30 March")
  5. Neighbours on the same postcode     (shared round; same lorry, usually same days)
  6. Our inference from a few dates      (needs evidence, see MIN_GAPS)
  7. Defaults                            (colour/purpose by category)

What matters most, for a reminder service, in order:
  which bin goes out > on which date > what day it usually is > how often > what goes in it
  > what colour it is. So: a missing bin or a wrong date is a blocking-grade problem; a
  frequency we can't prove is shown as "still checking" rather than guessed.
"""
from collections import Counter
from datetime import date

from app.services.profiles import CouncilProfile, profile_for

FREQ_BY_DAYS = {7: "weekly", 14: "fortnightly", 21: "three_weekly", 28: "four_weekly"}
WORDS = {7: "every week", 14: "every 2 weeks", 21: "every 3 weeks", 28: "every 4 weeks"}
MIN_GAPS = 2  # 3 dates with the same spacing before we claim a frequency from dates alone
RECYCLING_LIKE = {"recycling", "paper", "glass"}


def _snap(gap: int) -> int | None:
    """14 or 13/15 (bank holiday shift) -> 14. Anything else -> None."""
    for d in FREQ_BY_DAYS:
        if abs(gap - d) <= 2:
            return d
    return None


def observe(dates: list[date], days_per_week: int = 1) -> tuple[int | None, int]:
    """(interval in days, how many gaps support it) from concrete dates."""
    ds = sorted(set(dates))
    if len(ds) < 2:
        return None, 0
    if days_per_week > 1:  # twice-weekly: the pattern is weekly, evidence = weeks seen
        return 7, max(0, len(ds) // days_per_week - 1)
    snapped = [_snap((b - a).days) for a, b in zip(ds, ds[1:])]
    counted = Counter(s for s in snapped if s)
    if not counted:
        return None, 0
    interval, n = counted.most_common(1)[0]
    return interval, n


def run(bins: list[dict], council_name: str, gss_code: str | None,
        today: date | None = None) -> list[dict]:
    """
    Mutates each bin's "frequency" (+ adds "frequency_source") and returns the checks.
    Each bin needs: category, label, days, history (all known dates, past and future).
    """
    prof: CouncilProfile | None = profile_for(gss_code, today)
    by_cat = {s.category: s for s in prof.streams} if prof else {}
    checks: list[dict] = []

    for b in bins:
        interval, evidence = observe(b.get("history", []), max(1, len(b.get("days", []))))
        policy = by_cat.get(b["category"])
        policy_days = policy.every_days if policy else None

        if interval and evidence >= MIN_GAPS:
            b["frequency"], b["frequency_source"] = FREQ_BY_DAYS[interval], "calendar"
        elif policy_days and (interval is None or interval == policy_days):
            b["frequency"], b["frequency_source"] = FREQ_BY_DAYS[policy_days], "policy"
        else:
            b["frequency"], b["frequency_source"] = "irregular", "unknown"

        if policy_days and interval and interval != policy_days:
            checks.append({
                "kind": "conflict", "bin_type_id": b.get("bin_type_id"), "category": b["category"],
                "text": f"{council_name} says {policy.name} is collected {WORDS[policy_days]}, "
                        f"but its calendar for this address shows {WORDS[interval]}. "
                        "BinBot will keep checking. Fix it below if you know better.",
            })

    have = {b["category"] for b in bins}
    if prof:
        for s in prof.streams:
            if s.optional or s.category in have:
                continue
            checks.append({
                "kind": "missing_stream", "category": s.category, "every_days": s.every_days,
                "text": f"{council_name} also collects {s.name}"
                        + (f" {WORDS[s.every_days]}" if s.every_days in WORDS else "")
                        + ", but it isn't on the calendar for this address.",
            })
    elif bins and not (have & RECYCLING_LIKE):
        checks.append({
            "kind": "missing_stream", "category": "recycling", "every_days": None,
            "text": f"{council_name} doesn't list a recycling collection for this address.",
        })
    return checks
