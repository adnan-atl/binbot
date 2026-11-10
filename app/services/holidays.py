"""Council-wide Christmas and New Year changes (app/data/holiday_moves.json) for calendars
residents enter by hand. Stored into the rule's "moves" when it's saved, so the form shows them
filled in and the resident can still change any of them."""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _table() -> dict:
    path = Path(__file__).resolve().parent.parent / "data" / "holiday_moves.json"
    return json.loads(path.read_text(encoding="utf-8"))


def council_moves(gss: str | None, category: str) -> dict:
    """{usual date: new date, or None for no collection} for this council and kind of bin."""
    entry = _table().get(gss or "")
    if not entry:
        return {}
    cat = entry.get("categories", {}).get(category, {})
    moves = dict(cat.get("moves", entry.get("moves", {})))
    moves.update({d: None for d in cat.get("skip", [])})
    return moves
