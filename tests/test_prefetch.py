"""After a fresh council lookup, the neighbouring postcodes' calendars are fetched in the
background, so the next people from the street load instantly (services/prefetch.py)."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.adapters.base import RawCollection
from app.models import Property, Snapshot, User
from app.services import geo, prefetch, schedule
from app.services.geo import AddressHit
from app.services.schedule import postcode_round_key, round_has_calendar

NEAR = ["E8 3PD", "E8 3PE", "E8 3PF"]


@pytest.fixture
def street(monkeypatch):
    fetched = []

    def fetch(council, prop):
        fetched.append(prop.postcode)
        d = date.today() + timedelta(days=2)
        return [RawCollection("Refuse", d + timedelta(weeks=i)) for i in range(4)]

    paid = []

    def addresses(pc, paid_ok=True):
        paid.append(paid_ok)
        return [AddressHit(uprn=f"9{abs(hash(pc)) % 10**8}", line=f"1, Some Road, {pc}",
                           house_number="1")]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    monkeypatch.setattr(geo, "lookup_addresses", addresses)
    monkeypatch.setattr(prefetch, "nearby_postcodes", lambda pc, gss, limit: NEAR[:limit])
    return {"fetched": fetched, "paid": paid}


def _first(client, db):
    uid = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "100"}).json()["user_id"]
    return db.get(User, uid).property


def test_neighbouring_postcodes_get_their_own_calendars(client, db, street, monkeypatch):
    monkeypatch.setenv("PREFETCH_NEIGHBOURS", "2")
    from app.core.config import get_settings
    get_settings.cache_clear()
    prop = _first(client, db)
    street["fetched"].clear()
    assert prefetch.run(prop.id) == 2  # capped
    assert street["fetched"] == ["E8 3PD", "E8 3PE"]  # each looked up for itself
    assert street["paid"] and not any(street["paid"])  # free address provider only
    db.expire_all()
    for pc in ("E8 3PD", "E8 3PE"):
        assert round_has_calendar(db, postcode_round_key(prop.council, pc))
    assert prefetch.run(prop.id) == 1  # the two done are skipped; the next one is fetched
    # A neighbour on a prefetched postcode loads instantly: the calendar is reused
    j = client.post("/api/signup/start", json={"postcode": "E8 3PD", "uprn": "200"}).json()
    assert j["shared"] is True and j["source"] == "neighbours"


def test_weekly_refresh_skips_rounds_nobody_has_joined(client, db, street):
    from app.workers import monitor

    prop = _first(client, db)
    prefetch.run(prop.id)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    for s in db.scalars(select(Snapshot)):
        s.fetched_at = old
    db.commit()
    street["fetched"].clear()
    monitor.refresh_stale(db)
    assert set(street["fetched"]) == {"E8 3PB"}  # the resident's round, not the prefetched ones


def test_off_switch(client, db, street, monkeypatch):
    monkeypatch.setenv("PREFETCH_NEIGHBOURS", "0")
    from app.core.config import get_settings
    get_settings.cache_clear()
    started = []
    monkeypatch.setattr(prefetch.threading, "Thread", lambda **kw: started.append(kw))
    prefetch.start(_first(client, db).id)
    assert started == []
