"""The hand-copied Chichester calendars, with the council's holiday table, give exactly the
dates on the council's own three-month calendars (screenshots, 10 Nov 2026)."""
import json
from datetime import date
from pathlib import Path

from sqlalchemy import select

from app.adapters.manual import expand_rule
from app.models import User, UserBinSetting
from app.services import holidays
from app.tools import seed_rounds

ROUNDS = {r["postcode"]: r for r in json.loads(
    (Path(seed_rounds.__file__).resolve().parent.parent / "data" / "manual_rounds.json")
    .read_text())["rounds"]}


def d(*mds):
    return [date(2027 if m == 1 else 2026, m, dd) for m, dd in mds]


COUNCIL = {
    "PO19 3DX": {
        "General waste": d((11, 10), (11, 24), (12, 8), (12, 22), (1, 6), (1, 19)),
        "Recycling": d((11, 17), (12, 1), (12, 15), (12, 30), (1, 12), (1, 26)),
        "Food waste": d((11, 13), (11, 20), (11, 27), (12, 4), (12, 11), (12, 18), (12, 28),
                        (1, 4), (1, 9), (1, 15), (1, 22), (1, 29)),
        "Garden waste": d((11, 20), (12, 4), (12, 18), (1, 15), (1, 29))},
    "RH14 0JT": {
        "Recycling": d((11, 12), (11, 26), (12, 10), (12, 24), (1, 8), (1, 21)),
        "General waste": d((11, 19), (12, 3), (12, 17), (1, 2), (1, 14), (1, 28)),
        "Food waste": d((11, 16), (11, 23), (11, 30), (12, 7), (12, 14), (12, 21), (12, 29),
                        (1, 5), (1, 11), (1, 18), (1, 25))},
    "PO18 0EP": {
        "General waste": d((11, 11), (11, 25), (12, 9), (12, 23), (1, 7), (1, 20)),
        "Recycling": d((11, 18), (12, 2), (12, 16), (12, 31), (1, 13), (1, 27)),
        "Food waste": d((11, 11), (11, 18), (11, 25), (12, 2), (12, 9), (12, 16), (12, 23),
                        (12, 31), (1, 7), (1, 13), (1, 20), (1, 27)),
        "Garden waste": d((11, 17), (12, 1), (12, 15), (1, 12), (1, 26))},
    "PO10 8AP": {
        "Recycling": d((11, 10), (11, 24), (12, 8), (12, 22), (1, 6), (1, 19)),
        "General waste": d((11, 17), (12, 1), (12, 15), (12, 30), (1, 12), (1, 26)),
        "Food waste": d((11, 11), (11, 18), (11, 25), (12, 2), (12, 9), (12, 16), (12, 23),
                        (12, 31), (1, 7), (1, 13), (1, 20), (1, 27))},
    "PO10 8HW": {
        "Food waste": d((11, 11), (11, 18), (11, 25), (12, 2), (12, 9), (12, 16), (12, 23),
                        (12, 31), (1, 7), (1, 13), (1, 20), (1, 27)),
        "Garden waste": d((11, 12), (11, 26), (12, 10), (1, 7), (1, 21)),
        "General waste": d((11, 16), (11, 30), (12, 14), (12, 29), (1, 11), (1, 25)),
        "Recycling": d((11, 23), (12, 7), (12, 21), (1, 5), (1, 18))},
}


def test_every_bin_matches_the_council_calendar():
    start = date(2026, 11, 10)
    horizon = (date(2027, 1, 31) - start).days
    for pc, bins in COUNCIL.items():
        mine = {b["label"]: b for b in ROUNDS[pc]["bins"]}
        assert set(mine) == set(bins), pc
        for label, want in bins.items():
            b = mine[label]
            rule = {**b, "moves": holidays.council_moves("E07000225", b["category"])}
            assert expand_rule(rule, start, horizon) == want, (pc, label)


def test_seeded_calendar_reaches_a_real_sign_up(client, db):
    rounds = [{**ROUNDS["PO10 8HW"], "postcode": "E8 3PB"}]
    seed_rounds.load(db, rounds)
    seed_rounds.load(db, rounds)  # again: replaced, not doubled
    db.commit()
    seeds = db.scalars(select(User).where(User.lookup_source == "seed")).all()
    assert len(seeds) == 1
    assert len(db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == seeds[0].id)).all()) == 4
    # The demo council would answer; pretend it can't be asked, like Chichester
    from app.models import CollectionEvent
    from sqlalchemy import delete
    db.execute(delete(CollectionEvent)); db.commit()
    r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000009"}).json()
    assert r["source"] == "neighbours" and not r["error"]
    assert sorted(b["label"] for b in r["bins"]) == ["Food waste", "Garden waste", "General waste", "Recycling"]


def test_council_moves_fill_in_when_a_resident_saves(client, db, monkeypatch):
    monkeypatch.setattr(holidays, "council_moves",
                        lambda gss, cat: {"2026-12-25": "2026-12-28"} if cat == "food" else {})
    uid = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()["user_id"]
    out = client.put(f"/api/users/{uid}/bins", json={"bins": [
        {"label": "Food waste", "category": "food", "days": ["FRI"], "frequency": "weekly"}]}).json()
    food = next(b for b in out if b["label"] == "Food waste")
    assert food["moves"] == {"2026-12-25": "2026-12-28"}
    # They undo it: the usual date is kept and wins over the council's move
    out = client.put(f"/api/users/{uid}/bins", json={"bins": [
        {"setting_id": food["setting_id"], "label": "Food waste", "category": "food", "days": ["FRI"],
         "frequency": "weekly", "moves": {"2026-12-25": "2026-12-25"}}]}).json()
    assert next(b for b in out if b["label"] == "Food waste")["moves"] == {"2026-12-25": "2026-12-25"}
