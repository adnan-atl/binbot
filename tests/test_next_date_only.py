"""Councils that publish only each bin's next date (West Oxfordshire, Cotswold, Lambeth):
checked every two days so the following collection is known in time, and how often a bin
goes out is learned from the council's other postcodes instead of asking each resident."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.adapters.base import RawCollection
from app.models import CollectionEvent, Property, Snapshot, User, UserStatus
from app.services import schedule
from app.services.profiles import profile_for
from app.workers import monitor

TODAY = date.today()
NEXT = TODAY + timedelta(days=3)


def _signup(client, db, monkeypatch, items):
    calls = []

    def fetch(council, prop):
        calls.append(prop.uprn)
        return items

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    uid = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()["user_id"]
    user = db.get(User, uid)
    user.status, user.phone_e164 = UserStatus.active, "+447700900001"
    db.commit()
    calls.clear()
    return user, calls


def _age_snapshots(db, days):
    for s in db.scalars(select(Snapshot)):
        s.fetched_at = datetime.now(timezone.utc) - timedelta(days=days)
    db.commit()


def test_a_next_date_only_round_is_checked_again_after_two_days(client, db, monkeypatch):
    user, calls = _signup(client, db, monkeypatch, [RawCollection("Refuse", NEXT), RawCollection("Food", NEXT)])
    _age_snapshots(db, 1)
    monitor.refresh_stale(db)
    assert calls == []
    _age_snapshots(db, 3)
    monitor.refresh_stale(db)
    assert calls[0] == "10000002"  # (the weekly neighbour check may follow)


def test_a_round_with_dates_further_ahead_keeps_the_weekly_check(client, db, monkeypatch):
    items = [RawCollection("Refuse", NEXT + timedelta(days=14 * i)) for i in range(3)]
    user, calls = _signup(client, db, monkeypatch, items)
    _age_snapshots(db, 3)
    monitor.refresh_stale(db)
    assert calls == []


def test_how_often_is_learned_from_the_councils_other_postcodes(client, db, monkeypatch):
    user, _ = _signup(client, db, monkeypatch, [RawCollection("Refuse", NEXT)])
    prop = user.property
    bt = db.scalar(select(CollectionEvent.bin_type_id).where(CollectionEvent.round_key == prop.round_key))
    refuse = lambda: next(b for b in schedule.schedule_for(db, prop) if b["bin_type_id"] == bt)
    assert refuse()["frequency"] == "irregular"  # one date, nothing to go on

    # Two other postcodes in the council, watched for a few weeks: every 14 days.
    for n, key in enumerate(("pc:test:AA1 1AA", "pc:test:BB2 2BB")):
        for i in range(3):
            db.add(CollectionEvent(round_key=key, bin_type_id=bt,
                                   date=TODAY - timedelta(days=14 * i + n)))
    db.commit()
    assert schedule.learned_interval(db, bt, prop.round_key) == 14
    got = refuse()
    assert got["frequency"] == "fortnightly" and got["frequency_source"] == "learned"


def test_postcodes_that_disagree_teach_nothing(client, db, monkeypatch):
    user, _ = _signup(client, db, monkeypatch, [RawCollection("Refuse", NEXT)])
    bt = db.scalar(select(CollectionEvent.bin_type_id))
    for key, step in (("pc:test:AA1 1AA", 14), ("pc:test:BB2 2BB", 7)):
        for i in range(3):
            db.add(CollectionEvent(round_key=key, bin_type_id=bt, date=TODAY - timedelta(days=step * i)))
    db.commit()
    assert schedule.learned_interval(db, bt, user.property.round_key) is None


def test_stated_frequencies_for_the_three_councils():
    wodc = {s.category: s.every_days for s in profile_for("E07000181").streams}
    assert wodc == {"general": 14, "recycling": 14, "food": 7, "garden": 14}
    assert {s.category: s.every_days for s in profile_for("E07000079").streams}["food"] == 7
    lambeth = {s.category: s.every_days for s in profile_for("E09000022").streams}
    assert lambeth["general"] is None and lambeth["recycling"] == 7
