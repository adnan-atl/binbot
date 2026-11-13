"""A resident who only tells us how often a council bin goes out (the council publishes just
the next date) still gets the council's real dates: a bank holiday move reaches them. One who
changed the day itself has told us the council is wrong for their home, and keeps their day."""
from datetime import date, timedelta

from app.adapters.base import RawCollection
from app.models import User
from app.services import schedule
from app.services.reminders import effective_collections

TODAY = date.today()
FRI1 = TODAY + timedelta(days=(4 - TODAY.weekday()) % 7 or 7)
FRI2, FRI3 = FRI1 + timedelta(days=7), FRI1 + timedelta(days=14)
SAT2 = FRI2 + timedelta(days=1)


def _setup(client, db, monkeypatch, days):
    state = {"items": [RawCollection("Refuse", FRI1)]}
    monkeypatch.setattr(schedule, "fetch_collections", lambda council, prop: state["items"])
    r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    b = r["bins"][0]
    put = client.put(f"/api/users/{r['user_id']}/bins", json={"removed": [], "bins": [
        {"setting_id": b["setting_id"], "label": b["label"], "category": b["category"],
         "days": days, "frequency": "weekly"}]})
    assert put.status_code == 200, put.text
    user = db.get(User, r["user_id"])
    # The council's next answer: the second Friday's collection has moved to the Saturday.
    state["items"] = [RawCollection("Refuse", FRI1), RawCollection("Refuse", SAT2)]
    schedule.refresh_round(db, user.property)
    return user


def test_how_often_only_still_follows_the_councils_moved_date(client, db, monkeypatch):
    user = _setup(client, db, monkeypatch, ["FRI"])
    dates = list(effective_collections(db, user))
    assert FRI1 in dates and SAT2 in dates and FRI2 not in dates and FRI3 in dates
    shown = client.get(f"/api/users/{user.id}/bins").json()[0]
    assert SAT2.isoformat() in shown["upcoming"] and FRI2.isoformat() not in shown["upcoming"]


def test_a_resident_who_changed_the_day_keeps_it(client, db, monkeypatch):
    user = _setup(client, db, monkeypatch, ["MON"])
    dates = list(effective_collections(db, user))
    assert dates and all(d.weekday() == 0 for d in dates)


def test_a_date_the_resident_moved_themselves_is_left_alone(client, db, monkeypatch):
    user = _setup(client, db, monkeypatch, ["FRI"])
    s = client.get(f"/api/users/{user.id}/bins").json()[0]
    thu2 = FRI2 - timedelta(days=1)
    client.put(f"/api/users/{user.id}/bins", json={"removed": [], "bins": [
        {"setting_id": s["setting_id"], "label": s["label"], "category": s["category"],
         "days": ["FRI"], "frequency": "weekly", "moves": {FRI2.isoformat(): thu2.isoformat()}}]})
    db.expire_all()
    dates = list(effective_collections(db, db.get(User, user.id)))
    assert thu2 in dates and FRI2 not in dates
