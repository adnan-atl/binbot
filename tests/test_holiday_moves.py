"""Christmas and New Year: a resident can move or cancel single dates in their own rule."""
from datetime import date

from app.adapters.manual import clean_moves, expand_rule

FRI_WEEKLY = {"days": ["FRI"], "frequency": "weekly"}


def test_moves_and_cancels():
    rule = {**FRI_WEEKLY, "moves": {"2026-12-25": "2026-12-28", "2027-01-01": None}}
    got = expand_rule(rule, date(2026, 12, 20), 20)
    assert got == [date(2026, 12, 28), date(2027, 1, 8)]


def test_a_date_just_outside_the_window_can_move_in():
    rule = {"days": ["THU"], "frequency": "fortnightly", "anchor": "2026-12-17",
            "moves": {"2026-12-31": "2027-01-02"}}
    assert expand_rule(rule, date(2027, 1, 1), 10) == [date(2027, 1, 2)]


def test_no_moves_keeps_the_old_phase():
    rule = {"days": ["TUE"], "frequency": "fortnightly"}
    start = date(2026, 11, 10)
    assert expand_rule({**rule, "moves": {"2030-01-01": None}}, start, 60) == expand_rule(rule, start, 60)


def test_clean_moves_drops_nonsense():
    assert clean_moves({"2026-12-25": "2026-12-28", "2027-01-01": None, "bad": "2027-01-02",
                        "2026-12-01": "2027-03-01", "2026-12-10": "2026-12-10"}) == \
        {"2026-12-25": "2026-12-28", "2027-01-01": None}


def test_saved_through_the_site_and_shown_back(client):
    r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"})
    uid = r.json()["user_id"]
    r = client.put(f"/api/users/{uid}/bins", json={"bins": [
        {"label": "Food waste", "category": "food", "days": ["FRI"], "frequency": "weekly",
         "moves": {"2026-12-25": "2026-12-28"}}]})
    assert r.status_code == 200, r.text
    food = next(b for b in r.json() if b["label"] == "Food waste")
    assert food["moves"] == {"2026-12-25": "2026-12-28"} and food["manual"] is True
