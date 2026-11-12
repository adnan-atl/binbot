"""A stored calendar where one bin has run out of dates (the council only publishes the next
collection) isn't handed to a new neighbour as it is: we ask the council again."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.models import CollectionEvent, Property, Snapshot, User, UserBinSetting
from app.services.schedule import lapsed_bins, round_is_current
from app.tools import refresh_lapsed


def _start(client, uprn):
    r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": uprn})
    assert r.status_code == 200, r.text
    return r.json()


def _lapse_one_bin(db, round_key):
    """Leave one bin with a single date, yesterday; the last lookup was three days ago."""
    events = db.scalars(select(CollectionEvent).where(CollectionEvent.round_key == round_key)
                        .order_by(CollectionEvent.date)).all()
    bt = events[0].bin_type_id
    mine = [e for e in events if e.bin_type_id == bt]
    for e in mine[1:]:
        db.delete(e)
    mine[0].date = date.today() - timedelta(days=1)
    for s in db.scalars(select(Snapshot).where(Snapshot.round_key == round_key)):
        s.fetched_at = datetime.now(timezone.utc) - timedelta(days=3)
        s.content_hash = "three days ago"  # the dates have moved on since
    db.commit()
    return bt


def _round(db, user_id):
    return db.get(User, user_id).property.round_key


def test_new_neighbour_gets_every_bin_when_one_ran_out_of_dates(client, db):
    first = _start(client, "10000002")
    key = _round(db, first["user_id"])
    assert round_is_current(db, key)
    bt = _lapse_one_bin(db, key)
    assert list(lapsed_bins(db, key)) == [bt] and not round_is_current(db, key)

    second = _start(client, "10000003")
    assert not second["error"] and second["shared"] is False
    assert sorted(b["label"] for b in second["bins"]) == sorted(b["label"] for b in first["bins"])
    assert round_is_current(db, key)


def test_a_bin_already_out_of_dates_at_the_last_lookup_is_not_asked_about_again(client, db):
    first = _start(client, "10000002")
    key = _round(db, first["user_id"])
    _lapse_one_bin(db, key)
    for s in db.scalars(select(Snapshot).where(Snapshot.round_key == key)):
        s.fetched_at = datetime.now(timezone.utc)
    db.commit()
    assert round_is_current(db, key)
    second = _start(client, "10000003")
    assert second["shared"] is True and second["source"] == "neighbours"


def test_someone_who_joined_while_a_bin_had_no_dates_gets_it_back(client, db):
    first = _start(client, "10000002")
    key = _round(db, first["user_id"])
    bt = _lapse_one_bin(db, key)
    # Joined during the gap (before this fix): never offered the bin.
    prop = db.scalar(select(Property).where(Property.round_key == key))
    late = User(referral_code="late0001", property_id=prop.id)
    db.add(late)
    db.commit()
    # The first resident joined while it was showing, and removed it: stays removed.
    db.get(User, first["user_id"]).created_at = datetime.now(timezone.utc) - timedelta(days=5)
    for s in db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == first["user_id"],
                                                      UserBinSetting.bin_type_id == bt)):
        db.delete(s)
    db.commit()

    assert refresh_lapsed.run(db, apply=False) and not round_is_current(db, key)
    lines = refresh_lapsed.run(db, apply=True)
    assert "1 bin(s) restored" in lines[0], lines
    has = lambda uid: db.scalar(select(UserBinSetting.id).where(
        UserBinSetting.user_id == uid, UserBinSetting.bin_type_id == bt)) is not None
    assert has(late.id) and not has(first["user_id"])
