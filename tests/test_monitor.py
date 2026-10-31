"""Stage 2: diffing, change propagation, adapter breakage. Uses a fake adapter, no network."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.adapters.base import AdapterError, RawCollection
from app.models import ChangeLog, CollectionEvent, CouncilStatus, Outbox, Reminder, User
from app.services import schedule
from app.workers import monitor


@pytest.fixture
def fake_adapter(monkeypatch):
    """Replace the council fetch with a controllable list of collections."""
    state = {"items": [], "error": None}

    def fetch(council, prop):
        if state["error"]:
            raise state["error"]
        return state["items"]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    # Leave demo_mode on for postcode/address, but route fetching through the fake.
    return state


def _signup_active(client, db):
    uid = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123"})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return db.get(User, uid)


def test_change_detected_and_user_notified(client, db, fake_adapter):
    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)
    fake_adapter["items"] = [RawCollection("Recycling", tue + timedelta(weeks=i)) for i in range(4)]
    user = _signup_active(client, db)
    prop = user.property
    assert db.scalar(select(ChangeLog)) is None  # first fetch is not a "change"

    # Council moves one collection by a day (bank holiday).
    moved = tue + timedelta(weeks=2)
    fake_adapter["items"] = [
        RawCollection("Recycling", d + (timedelta(days=1) if d == moved else timedelta()))
        for d in (tue + timedelta(weeks=i) for i in range(4))
    ]
    res = schedule.refresh_round(db, prop)
    assert res.changed and res.diff["moved"] and not res.diff["added"] and not res.diff["removed"]
    ev = db.scalar(select(CollectionEvent).where(CollectionEvent.date == moved + timedelta(days=1)))
    assert ev.moved_from == moved

    # Monitor propagates: reminders rebuilt, user told.
    monitor._propagate_change(db, prop, res.diff)
    db.commit()
    msg = db.scalar(select(Outbox).where(Outbox.kind == "change"))
    assert msg and "moved a collection" in msg.body
    assert db.scalar(select(Reminder).where(Reminder.collection_date == moved + timedelta(days=1)))
    assert db.scalar(select(Reminder).where(Reminder.collection_date == moved)) is None
    log = db.scalar(select(ChangeLog).where(ChangeLog.kind == "dates_changed"))
    assert log and log.users_notified == 1


def test_unchanged_fetch_is_noop(client, db, fake_adapter):
    tue = date.today() + timedelta(days=7)
    fake_adapter["items"] = [RawCollection("Refuse", tue)]
    user = _signup_active(client, db)
    res = schedule.refresh_round(db, user.property)
    assert not res.changed and db.scalar(select(ChangeLog)) is None


def test_adapter_breakage_flags_council_and_keeps_old_dates(client, db, fake_adapter):
    tue = date.today() + timedelta(days=7)
    fake_adapter["items"] = [RawCollection("Refuse", tue)]
    user = _signup_active(client, db)
    council = user.property.council
    fake_adapter["error"] = AdapterError("site changed", retryable=True)
    for _ in range(3):
        schedule.refresh_round(db, user.property)
    db.refresh(council)
    assert council.status == CouncilStatus.broken and council.failure_count == 3
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "adapter_broken"))
    # Old events survive a failed fetch, so reminders keep going.
    assert db.scalar(select(CollectionEvent).where(CollectionEvent.date == tue))

    fake_adapter["error"] = None
    monitor.health_check(db)
    db.refresh(council)
    assert council.status == CouncilStatus.ok
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "adapter_recovered"))


def test_refresh_stale_polls_harder_near_bank_holiday():
    assert monitor.near_bank_holiday(date(2026, 12, 10), 21)
    assert not monitor.near_bank_holiday(date(2026, 10, 1), 21)
