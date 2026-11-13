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
    # West Oxfordshire, Cotswold and Lambeth publish only each bin's next date, so the calendar
    # can't show how often a bin goes out until we've watched it for a few weeks. Their own
    # documents say. Flats with communal bins, and homes on sacks, can be on a different
    # frequency: this is only what's shown, reminders always follow the council's dates.
    "E07000181": [
        CouncilProfile(
            effective_from=date(2026, 4, 2),
            source="https://westoxon.gov.uk/media/5ljhjuja/wodc-waste-and-recycling-policy-2026.pdf",
            streams=(
                Stream("general", "refuse", 14),
                Stream("recycling", "dry recycling", 14),
                Stream("food", "food waste", 7),
                Stream("garden", "garden waste", 14, optional=True),
            ),
            note="Standard homes. Communal bins and homes with no storage: refuse and recycling weekly.",
        ),
    ],
    "E07000079": [
        CouncilProfile(
            effective_from=date(2023, 2, 1),
            source="https://www.cotswold.gov.uk/media/ebwpghck/cdc-waste-recycling-street-cleaning-policy.pdf",
            streams=(
                Stream("general", "residual household waste", 14),
                Stream("recycling", "recycling", 14),
                Stream("food", "food waste", 7),
                Stream("garden", "garden waste", None, optional=True),
            ),
            note="Flats may have communal bins or a bag service; no frequency stated for those.",
        ),
    ],
    "E09000022": [
        CouncilProfile(
            effective_from=date(2024, 4, 1),
            source="https://lambeth.gov.uk/rubbish-recycling/fortnightly-rubbish-collections",
            streams=(
                # Black bins went fortnightly for street-level homes only; communal bins and
                # sacks did not change, and the label doesn't tell them apart: not stated.
                Stream("general", "black bin rubbish", None),
                Stream("recycling", "recycling", 7),
                Stream("food", "food waste", 7),
                Stream("garden", "garden waste", 7, optional=True),
            ),
        ),
    ],
}


def profile_for(gss_code: str | None, on: date | None = None) -> CouncilProfile | None:
    """The profile in force on `on` (default today), or None if we have none."""
    on = on or date.today()
    live = [p for p in PROFILES.get(gss_code or "", []) if p.effective_from <= on]
    return max(live, key=lambda p: p.effective_from) if live else None
