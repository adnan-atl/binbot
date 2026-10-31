"""Map council bin labels to canonical categories, and events to a human rule."""
import re
from collections import Counter
from datetime import date

from app.models import BinCategory, Frequency

# Order matters: first match wins. Keep specific before generic.
_RULES: list[tuple[BinCategory, tuple[str, ...]]] = [
    (BinCategory.food, ("food", "caddy", "kitchen", "organic waste")),
    (BinCategory.garden, ("garden", "green waste", "brown bin", "compost", "grass")),
    (BinCategory.glass, ("glass", "bottle bank")),
    (BinCategory.paper, ("paper", "card", "cardboard")),
    (
        BinCategory.recycling,
        ("recycl", "mixed", "blue", "dry", "plastic", "cans", "tins", "co-mingled", "comingled"),
    ),
    (
        BinCategory.general,
        ("general", "refuse", "residual", "rubbish", "household", "black", "grey", "gray",
         "landfill", "non-recycl", "domestic waste", "waste bin"),
    ),
]

_COLOURS = ("blue", "green", "brown", "black", "grey", "gray", "purple", "red", "yellow",
            "orange", "white", "silver", "burgundy", "maroon", "pink")


# What a bin colour means differs by council: green is garden waste in most of England and
# general waste in Glasgow. When a council labels bins by colour alone, look here first.
# Keyed by GSS code. Add a council when its labels come through as bare colours.
COUNCIL_COLOURS: dict[str, dict[str, BinCategory]] = {
    "S12000049": {  # Glasgow City
        "blue": BinCategory.recycling, "green": BinCategory.general, "grey": BinCategory.food,
        "brown": BinCategory.garden, "purple": BinCategory.glass,
    },
    "E09000013": {  # Hammersmith & Fulham (sacks/bins by colour)
        "blue": BinCategory.recycling, "black": BinCategory.general,
    },
}


def categorise(label: str, gss_code: str | None = None) -> BinCategory:
    l = label.lower()
    if gss_code and gss_code in COUNCIL_COLOURS:
        c = colour_of(l)
        if c and c in COUNCIL_COLOURS[gss_code]:
            return COUNCIL_COLOURS[gss_code][c]
    for cat, needles in _RULES:
        if any(n in l for n in needles):
            return cat
    return BinCategory.other


# Short "what goes in it" line for the UI when the council label is just a colour.
PURPOSE = {
    BinCategory.recycling: "Recycling: paper, card, cans, plastic bottles",
    BinCategory.general: "General waste",
    BinCategory.food: "Food waste",
    BinCategory.garden: "Garden waste",
    BinCategory.glass: "Glass",
    BinCategory.paper: "Paper and card",
    BinCategory.other: "",
}


def colour_of(label: str) -> str | None:
    l = label.lower()
    for c in _COLOURS:
        if re.search(rf"\b{c}\b", l):
            return "grey" if c == "gray" else c
    return None


_WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def describe_rule(dates: list[date]) -> dict:
    """
    Derive a display rule from concrete dates: which weekday(s) and how often.
    Purely for the UI. Dates stay the source of truth.
    """
    ds = sorted(set(dates))
    if not ds:
        return {"days": [], "frequency": Frequency.irregular.value, "next": None}
    days = [_WEEKDAYS[d.weekday()] for d in ds]
    common = [d for d, _ in Counter(days).most_common()]
    # Keep days that appear in at least a third of events (filters bank-holiday shifts).
    threshold = max(1, len(ds) / 3)
    kept = [d for d in common if days.count(d) >= threshold]
    kept.sort(key=_WEEKDAYS.index)

    gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
    freq = Frequency.irregular
    if gaps:
        typical = Counter(gaps).most_common(1)[0][0]
        per_week = len(kept) if kept else 1
        if per_week > 1 and typical <= 7:
            freq = Frequency.weekly
        elif 6 <= typical <= 8:
            freq = Frequency.weekly
        elif 13 <= typical <= 15:
            freq = Frequency.fortnightly
        elif 27 <= typical <= 29:
            freq = Frequency.four_weekly
    return {"days": kept, "frequency": freq.value, "next": ds[0].isoformat()}
