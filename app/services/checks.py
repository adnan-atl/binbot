"""
Checker: is what we're about to show someone actually right?

Internal only. Residents see one clean answer, picked by the hierarchy below; they never
see our doubts, source disagreements or "still checking". Every disagreement or gap goes
to change_log for ops to review and fix at the source (adapter, override, profile).

Sources, most trusted first. The highest source that has an answer is what's shown:

  1. The resident, for their own home   (they see the lorry; override_rule, handled upstream)
  2. Council calendar for this address   (scraped dates; specific and current)
  3. Council's stated rule on that page  ("emptied every 14 days"; Glasgow)
  4. Council service policy              (profiles.py: "3-weekly from 30 March")
  5. Neighbours on the same postcode     (shared round; same lorry, usually same days)
  6. Our inference from a few dates      (needs evidence, see MIN_GAPS)
  7. Defaults                            (colour/purpose by category)

What matters most, for a reminder service, in order:
  which bin goes out > on which date > what day it usually is > how often > what goes in it
  > what colour it is. A missing bin or a wrong date is the most urgent thing for ops to fix.
"""
from collections import Counter
from datetime import date

from app.services.profiles import CouncilProfile, profile_for

FREQ_BY_DAYS = {7: "weekly", 14: "fortnightly", 21: "three_weekly", 28: "four_weekly"}
WORDS = {7: "every week", 14: "every 2 weeks", 21: "every 3 weeks", 28: "every 4 weeks"}
MIN_GAPS = 2  # below this the calendar still wins for display, but a policy clash is logged
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
    Mutates each bin's "frequency" (+ adds "frequency_source") and returns the checks
    (for change_log, never for the resident).
    Each bin needs: category, label, days, history (all known dates, past and future).
    """
    prof: CouncilProfile | None = profile_for(gss_code, today)
    by_cat = {s.category: s for s in prof.streams} if prof else {}
    checks: list[dict] = []

    for b in bins:
        interval, evidence = observe(b.get("history", []), max(1, len(b.get("days", []))))
        policy = by_cat.get(b["category"])
        policy_days = policy.every_days if policy else None

        # Hierarchy: address calendar (2) > council policy (4) > nothing shown.
        if interval:
            b["frequency"], b["frequency_source"] = FREQ_BY_DAYS[interval], "calendar"
        elif policy_days:
            b["frequency"], b["frequency_source"] = FREQ_BY_DAYS[policy_days], "policy"
        else:
            b["frequency"], b["frequency_source"] = "irregular", "unknown"

        if policy_days and interval and interval != policy_days:
            checks.append({
                "kind": "conflict", "bin_type_id": b.get("bin_type_id"), "category": b["category"],
                "evidence": evidence, "calendar_days": interval, "policy_days": policy_days,
                "text": f"{council_name}: policy says {policy.name} {WORDS[policy_days]}, "
                        f"address calendar shows {WORDS[interval]} ({evidence + 1} dates).",
            })

    have = {b["category"] for b in bins}
    if prof:
        for s in prof.streams:
            if s.optional or s.category in have:
                continue
            checks.append({
                "kind": "missing_stream", "category": s.category, "every_days": s.every_days,
                "text": f"{council_name}: policy lists {s.name}"
                        + (f" {WORDS[s.every_days]}" if s.every_days in WORDS else "")
                        + ", address calendar has none.",
            })
    elif bins and not (have & RECYCLING_LIKE):
        checks.append({
            "kind": "missing_stream", "category": "recycling", "every_days": None,
            "text": f"{council_name}: no recycling on the address calendar.",
        })
    # Every council collects general waste. A calendar without it is a partial scrape (or a
    # bin we couldn't read): flag it so verify asks the neighbours, like missing recycling.
    if bins and "general" not in have and not any(c["category"] == "general" for c in checks):
        checks.append({
            "kind": "missing_stream", "category": "general", "every_days": None,
            "text": f"{council_name}: no general waste on the address calendar.",
        })
    return checks
