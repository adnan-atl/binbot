"""Colours councils publish but leave out of their collection data (app/data/council_colours.json)."""
from sqlalchemy import select

from app.models import User
from app.services import normalize
from app.services.normalize import council_colour
from app.services.reminders import effective_collections
from tests.test_share_nudge import _on


def test_hammersmith_and_fulham_recycling_is_green():
    assert council_colour("E09000013", "recycling") == "green"
    assert council_colour("E09000013", "general") == "black"
    assert council_colour("E09000013", "garden") is None  # not known: we ask
    assert council_colour("E99999999", "recycling") is None


def test_known_colour_is_shown_and_not_asked_for(client, db, monkeypatch):
    # Demo council is Hackney; its food caddy has no colour in its name
    monkeypatch.setattr(normalize, "_KNOWN", {"E09000012": {"food": "grey"}})
    uid = _on(client)
    bins = {b["category"]: b for b in client.get(f"/api/users/{uid}/bins").json()}
    assert bins["food"]["colour"] == "grey"
    db.expire_all()
    cal = effective_collections(db, db.get(User, uid))
    food = [b for day in cal.values() for b in day if b["category"] == "food"]
    assert food and all(b["colour"] == "grey" for b in food)
    assert normalize.bin_tag(food[0]).startswith("⚪")  # grey shows as the white dot


def test_residents_choice_wins(client, db, monkeypatch):
    monkeypatch.setattr(normalize, "_KNOWN", {"E09000012": {"food": "grey"}})
    uid = _on(client)
    bins = client.get(f"/api/users/{uid}/bins").json()
    body = {"bins": [{"setting_id": b["setting_id"], "label": b["label"], "category": b["category"],
                      "enabled": True, **({"colour": "brown"} if b["category"] == "food" else {})}
                     for b in bins], "removed": []}
    out = {b["category"]: b for b in client.put(f"/api/users/{uid}/bins", json=body).json()}
    assert out["food"]["colour"] == "brown"
