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
    assert council_colour("E09000013", "glass") is None  # not known: we ask
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


def _pick(client, uid, category, colour):
    bins = client.get(f"/api/users/{uid}/bins").json()
    body = {"bins": [{"setting_id": b["setting_id"], "label": b["label"], "category": b["category"],
                      "enabled": True, **({"colour": colour} if b["category"] == category else {})}
                     for b in bins], "removed": []}
    client.put(f"/api/users/{uid}/bins", json=body)


def test_one_neighbour_teaches_the_round(client, db):
    from tests.test_flow import signup
    first = signup(client)["user_id"]
    _pick(client, first, "food", "brown")
    second = signup(client)["user_id"]  # same demo postcode: same round
    bins = {b["category"]: b for b in client.get(f"/api/users/{second}/bins").json()}
    assert bins["food"]["colour"] == "brown"


def test_two_agreeing_residents_teach_the_council(client, db):
    from app.models import Property, User
    from app.services.schedule import resident_colour
    from app.models import BinType, UserBinSetting
    from tests.test_flow import signup
    users = [signup(client)["user_id"] for _ in range(3)]
    db.expire_all()
    # put the two pickers on other rounds of the same council
    for n, uid in enumerate(users[:2]):
        u = db.get(User, uid)
        u.property = Property(postcode="E8 1AA", round_key=f"pc:other{n}", council_id=u.property.council_id)
    db.commit()
    _pick(client, users[0], "food", "brown")
    db.expire_all()
    bt = db.get(BinType, db.scalar(select(UserBinSetting.bin_type_id).where(
        UserBinSetting.user_id == users[2], UserBinSetting.category == "food")))
    mine = db.get(User, users[2]).property.round_key
    assert resident_colour(db, bt, mine) is None  # one pick elsewhere isn't enough
    _pick(client, users[1], "food", "brown")
    db.expire_all()
    assert resident_colour(db, bt, mine) == "brown"


def test_only_the_councils_options_are_offered(client, db, monkeypatch):
    # Hackney (the demo council) uses grey or blue food caddies: ask between just those two
    monkeypatch.setattr(normalize, "_KNOWN", {"E09000012": {"options": {"food": ["grey", "blue"]}}})
    uid = _on(client)
    bins = {b["category"]: b for b in client.get(f"/api/users/{uid}/bins").json()}
    assert bins["food"]["colour"] is None and bins["food"]["colour_options"] == ["grey", "blue"]
    assert bins["general"]["colour_options"] == []  # nothing known: every colour


def test_options_drop_away_once_the_colour_is_known(client, db, monkeypatch):
    monkeypatch.setattr(normalize, "_KNOWN", {"E09000012": {"food": "grey", "options": {"food": ["grey", "blue"]}}})
    uid = _on(client)
    bins = {b["category"]: b for b in client.get(f"/api/users/{uid}/bins").json()}
    assert bins["food"]["colour"] == "grey" and bins["food"]["colour_options"] == []


def test_options_need_two_real_colours():
    from app.services.normalize import council_colour_options
    normalize._KNOWN = {"X": {"options": {"general": ["black", "beige"]}}}
    try:
        assert council_colour_options("X", "general") == []
    finally:
        normalize._KNOWN = None
