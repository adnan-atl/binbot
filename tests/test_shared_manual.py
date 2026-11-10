"""Days a resident types in by hand are shared with the rest of their postcode, for councils
we can't ask (Chichester, Canterbury) or that weren't answering."""
from sqlalchemy import delete, select

from app.models import CollectionEvent, Property, UserBinSetting


def _start(client, uprn):
    r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": uprn})
    assert r.status_code == 200, r.text
    return r.json()


def _no_council_calendar(db):
    db.execute(delete(CollectionEvent))
    db.commit()


def test_neighbour_gets_the_days_typed_in_by_hand(client, db):
    first = _start(client, "10000002")
    _no_council_calendar(db)
    r = client.put(f"/api/users/{first['user_id']}/bins", json={
        "removed": [b["setting_id"] for b in first["bins"]],
        "bins": [
            {"label": "General waste", "category": "general", "days": ["TUE"],
             "frequency": "fortnightly", "anchor": "2026-11-10", "colour": "black"},
            {"label": "Food waste", "category": "food", "days": ["FRI"], "frequency": "weekly"},
        ]})
    assert r.status_code == 200, r.text

    second = _start(client, "10000003")
    assert second["shared"] is True and second["source"] == "neighbours" and not second["error"]
    got = db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == second["user_id"])).all()
    assert sorted(s.label for s in got) == ["Food waste", "General waste"]
    general = next(s for s in got if s.label == "General waste")
    assert general.override_rule["anchor"] == "2026-11-10" and general.colour == "black"
    assert general.bin_type_id is None


def test_a_different_postcode_does_not_get_them(client, db):
    first = _start(client, "10000002")
    _no_council_calendar(db)
    client.put(f"/api/users/{first['user_id']}/bins", json={
        "removed": [b["setting_id"] for b in first["bins"]],
        "bins": [{"label": "General waste", "category": "general", "days": ["TUE"]}]})
    for p in db.scalars(select(Property)):
        p.round_key = "pc:other:X1 1XX"
    db.commit()
    second = _start(client, "10000003")
    labels = [b["label"] for b in second["bins"]]
    assert labels != ["General waste"]
