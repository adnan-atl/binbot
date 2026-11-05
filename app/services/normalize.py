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
    # "Non-Recyclable Refuse" (Bromley) contains "recycl": it's general waste, not recycling.
    if _NOT_RECYCLING.search(l):
        return BinCategory.general
    # "Mixed Recycling (Cans, Plastics & Glass)": a recycling bin that takes glass, not a glass box.
    if _MIXED.search(l) and not any(n in l for n in ("food", "garden", "compost")):
        return BinCategory.recycling
    for cat, needles in _RULES:
        if any(n in l for n in needles):
            return cat
    return BinCategory.other


_NOT_RECYCLING = re.compile(r"\b(non|not|un)[\s-]?recycl")
_MIXED = re.compile(r"\b(mixed|co-?mingled)\b.*\brecycl")


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


# One emoji per bin in messages: a dot in the bin's colour when we know it (that's how people
# think of their bins), otherwise one for the kind of bin. No grey dot exists, so grey is white.
COLOUR_DOTS = {"blue": "\U0001F535", "black": "\u26AB", "green": "\U0001F7E2", "brown": "\U0001F7E4",
               "grey": "\u26AA", "silver": "\u26AA", "white": "\u26AA", "purple": "\U0001F7E3",
               "burgundy": "\U0001F7E3", "maroon": "\U0001F7E3", "red": "\U0001F534",
               "yellow": "\U0001F7E1", "orange": "\U0001F7E0", "pink": "\U0001FA77"}
KIND_EMOJI = {"recycling": "\u267B\uFE0F", "general": "\U0001F5D1\uFE0F", "food": "\U0001F34E",
              "garden": "\U0001F342", "glass": "\U0001F37E", "paper": "\U0001F4E6"}


# When neither the council nor the bin's name gives a colour: the same defaults the website
# draws its bin icons with, so both show the same colour.
DEFAULT_COLOUR = {"recycling": "blue", "general": "black", "garden": "green", "food": "brown"}


def bin_emoji(label: str, category: str | None = None, colour: str | None = None) -> str:
    c = (colour or colour_of(label) or DEFAULT_COLOUR.get(category or "") or "").lower()
    return COLOUR_DOTS.get(c) or KIND_EMOJI.get(category or "", "\U0001F5D1\uFE0F")


def _cap(t: str) -> str:
    t = t.strip()
    return t[:1].upper() + t[1:]


def bin_name(label: str) -> str:
    """The colour first, the kind in brackets, each starting with a capital:
    'glass (black box or basket)' -> 'Black box or basket (Glass)',
    'Mixed dry recycling (blue lidded bin)' -> 'Blue lidded bin (Mixed dry recycling)'.
    Names without a bracketed colour just get a capital: 'household waste' -> 'Household waste'."""
    m = re.fullmatch(r"\s*(.+?)\s*\((.+)\)\s*", label)
    if m and colour_of(m.group(2)):
        return f"{_cap(m.group(2))} ({_cap(m.group(1))})"
    return _cap(label)


def bin_tag(b: dict) -> str:
    """'🔵 Blue lidded bin (Mixed dry recycling)' from a bin dict (label, category, colour)."""
    return f"{bin_emoji(b['label'], b.get('category'), b.get('colour'))} {bin_name(b['label'])}"


def colour_of(label: str) -> str | None:
    l = label.lower()
    for c in _COLOURS:
        if re.search(rf"\b{c}\b", l):
            return "grey" if c == "gray" else c
    return None


_WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

# A bin collected this many days a week or more (Westminster's daily rubbish, say) goes out
# every day: it's shown with a note but never reminded about, or there'd be a message every
# evening. The site's confirm step uses the same number (DAILY_MIN in the designs).
DAILY_MIN_DAYS = 5


def is_daily(days: list[str]) -> bool:
    return len(set(days)) >= DAILY_MIN_DAYS


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
    # Keep days that turn up often enough to be the pattern, not a bank-holiday shift. Count
    # against weeks as well as events: Westminster's daily rubbish is 14 events in 2 weeks,
    # 2 per weekday, which a per-event threshold (14/3) used to throw away entirely.
    weeks = (ds[-1] - ds[0]).days // 7 + 1
    threshold = max(1, min(len(ds), weeks) / 3)
    kept = [d for d in common if days.count(d) >= threshold]
    if not kept:  # never show a bin with dates but no day: fall back to the next dates' days
        kept = list(dict.fromkeys(_WEEKDAYS[d.weekday()] for d in ds[:7]))
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
        elif 20 <= typical <= 22:
            freq = Frequency.three_weekly
        elif 27 <= typical <= 29:
            freq = Frequency.four_weekly
    return {"days": kept, "frequency": freq.value, "next": ds[0].isoformat()}
