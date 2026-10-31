"""
Council service profiles: what a council says it collects, how often, and from when.

This is the council's *policy* (a news page, a "changes to your collections" page), not a
calendar. It is used to cross-check what a scraper returns, never to invent dates:
  - a stream the policy lists but the calendar doesn't  -> "missing stream" check
  - a frequency the calendar shows that the policy contradicts -> "conflict" check
  - a bin with too few dates to infer how often -> the policy's frequency, marked as such

Add a council when it announces a service change (3-weekly, 4-weekly, new food waste).
Each entry carries its source URL and the date it takes effect.
"""
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Stream:
    category: str          # BinCategory value: general, recycling, paper, glass, food, garden
    name: str              # how the council names it, for messages
    every_days: int | None  # 7, 14, 21, 28; None = not stated
    optional: bool = False  # paid or opt-in (garden subscription): absence is not a problem


@dataclass(frozen=True)
class CouncilProfile:
    effective_from: date
    source: str
    streams: tuple[Stream, ...] = field(default_factory=tuple)
    note: str = ""


PROFILES: dict[str, list[CouncilProfile]] = {
    # Lancaster: from 30 Mar 2026 grey bin, plastics/glass/cans and paper/card rotate,
    # one per week, each every 3 weeks. Food weekly, garden fortnightly (subscription).
    "E07000121": [
        CouncilProfile(
            effective_from=date(2026, 3, 30),
            source="https://www.lancaster.gov.uk/waste-and-recycling/household-waste/"
                   "changes-to-your-waste-and-recycling-collection-services/"
                   "changes-to-frequency-of-collections",
            streams=(
                Stream("general", "grey bin (non-recyclable waste)", 21),
                Stream("recycling", "plastics, glass and cans", 21),
                Stream("paper", "paper and cardboard", 21),
                Stream("food", "food waste caddy", 7),
                Stream("garden", "garden waste", 14, optional=True),
            ),
            note="Three-weekly rotation: one of grey / plastics-glass-cans / paper-card each week.",
        ),
    ],
}


def profile_for(gss_code: str | None, on: date | None = None) -> CouncilProfile | None:
    """The profile in force on `on` (default today), or None if we have none."""
    on = on or date.today()
    live = [p for p in PROFILES.get(gss_code or "", []) if p.effective_from <= on]
    return max(live, key=lambda p: p.effective_from) if live else None
