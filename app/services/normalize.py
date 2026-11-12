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
         "landfill", "non-recycl", "domestic waste", "waste bin",
         "waste"),  # last: plain "Waste" (Hammersmith & Fulham); food/garden waste match earlier
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
    # Lambeth's service names each container by colour only. Its own records say which is
    # which: "Domestic Recycling 240L wheelie bin" is the green bin, "Domestic Food Waste 23L
    # kerbside" the brown bin (not garden waste, which goes out in sacks).
    "E09000022": {
        "green": BinCategory.recycling, "brown": BinCategory.food, "black": BinCategory.general,
    },
}


def council_category(label: str, gss_code: str | None) -> BinCategory | None:
    """What this council means by a colour-only label, when we've been told (COUNCIL_COLOURS)."""
    if gss_code and gss_code in COUNCIL_COLOURS:
        return COUNCIL_COLOURS[gss_code].get(colour_of(label.lower()))
    return None


def categorise(label: str, gss_code: str | None = None) -> BinCategory:
    l = label.lower()
    told = council_category(label, gss_code)
    if told:
        return told
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


# The colours the website draws its bin icons with when it has nothing better. Never used to
# tell a resident what colour their bin is: that differs street by street (recycling is green
# on some Hammersmith & Fulham streets, blue on others), so a guess here would be wrong.
DEFAULT_COLOUR = {"recycling": "blue", "general": "black", "garden": "green", "food": "brown"}


def _council_colours() -> dict:
    """app/data/council_colours.json, read once."""
    global _KNOWN
    if _KNOWN is None:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "data" / "council_colours.json"
        try:
            _KNOWN = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _KNOWN = {}
    return _KNOWN


_KNOWN: dict | None = None


def council_colour(gss_code: str | None, category: str | None) -> str | None:
    """The colour a council publishes for this kind of bin (H&F: recycling is green), for when
    its collection data only says "Recycling". None when we don't know it: then we ask."""
    entry = _council_colours().get(gss_code or "") or {}
    c = entry.get(category or "")
    return c if c in COLOUR_DOTS else None


def split_recycling(gss_code: str | None) -> list[tuple[str, str]]:
    """Councils that collect recycling in several containers of different colours (Hounslow:
    red box, green box for glass, blue box for paper): [(colour, what it's for)], mixed
    recycling first. Empty when the council has one recycling colour, or none we know."""
    entry = _council_colours().get(gss_code or "") or {}
    if not isinstance(entry, dict):
        return []
    parts = [(entry.get(k), what) for k, what in
             (("recycling", ""), ("glass", "glass"), ("paper", "paper and card"))]
    parts = [(c, what) for c, what in parts if c in COLOUR_DOTS]
    return parts if len({c for c, _ in parts}) > 1 else []


def colours_text(parts: list[tuple[str, str]], detail: bool = False) -> str:
    """'red, green and blue', or with detail 'red, green (glass) and blue (paper and card)'."""
    words = ([f"{c} ({what})" if what else c for c, what in parts] if detail
             else list(dict.fromkeys(c for c, _ in parts)))
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def council_colour_options(gss_code: str | None, category: str | None) -> list[str]:
    """When a council uses one of a few colours for this bin depending on the street ("black or
    grey"), those colours, so we ask the resident to choose between just them. Empty when we
    don't know: then they get every colour."""
    entry = _council_colours().get(gss_code or "") or {}
    opts = [c for c in (entry.get("options") or {}).get(category or "", []) if c in COLOUR_DOTS]
    return opts if len(opts) >= 2 else []


def bin_emoji(label: str, category: str | None = None, colour: str | None = None) -> str:
    """A dot in the bin's colour when the council, the bin's name or the resident gives one;
    otherwise the kind of bin (♻️ recycling, 🗑️ general...), so we never show a colour
    we only guessed. A bin we can't place at all still gets a plain dot."""
    c = (colour or colour_of(label) or "").lower()
    return COLOUR_DOTS.get(c) or KIND_EMOJI.get(category or "", "\u26AB")


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


# Council names for the everyday rubbish bin. Without a colour of their own they're all shown
# as one name, so nobody sees "Household waste" with "General waste" underneath.
_GENERAL_NAMES = re.compile(
    r"(general|household|domestic|residual|non[\s-]?recyclable)?\s*(waste|refuse|rubbish)"
    r"(\s+(bin|collection|waste))*|non[\s-]?recyclable(\s+\w+)*|refuse|rubbish|waste", re.I)


def display_label(label: str, category: str | None = None) -> str:
    """How a bin is named to the resident, on the site and in messages (via bin_tag):
    the council's name with a capital, except the everyday bin without a colour of its own,
    which is always 'General waste' (no colour: we'd only be guessing it)."""
    if category == "general" and not colour_of(label) and _GENERAL_NAMES.fullmatch(label.strip()):
        return "General waste"
    return _cap(label)


def bin_tag(b: dict) -> str:
    """'🔵 Blue lidded bin (Mixed dry recycling)' from a bin dict (label, category, colour)."""
    label = display_label(b["label"], b.get("category"))
    if b.get("colours") and not b.get("colour"):
        # Several recycling containers go out together: no single colour would be right
        return f"{KIND_EMOJI['recycling']} {bin_name(label)} ({colours_text([(c, '') for c in b['colours']])})"
    return f"{bin_emoji(label, b.get('category'), b.get('colour'))} {bin_name(label)}"


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
