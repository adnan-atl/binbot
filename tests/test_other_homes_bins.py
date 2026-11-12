"""A postcode shares one calendar, but some bins belong to only some of its homes (a garden
subscription, a 120 litre bin where next door has 180). Refreshing from a home without one
mustn't wipe its dates: we ask the round's other homes for it. No network."""
from datetime import date, timedelta

from sqlalchemy import select

from app.adapters.base import AdapterError, RawCollection
from app.models import CollectionEvent, User
from app.services import schedule

TODAY = date.today()
WK1, WK2 = TODAY + timedelta(days=6), TODAY + timedelta(days=13)
A, B = "10000002", "10000003"


def _street(client, db, monkeypatch):
    """Home A has refuse only; home B has refuse and a garden bin. Same postcode round."""
    answers = {A: [RawCollection("Refuse", WK1)],
               B: [RawCollection("Refuse", WK1), RawCollection("Garden Bin", WK1)]}
    calls = []

    def fetch(council, prop):
        calls.append(prop.uprn)
        if isinstance(answers[prop.uprn], Exception):
            raise answers[prop.uprn]
        return answers[prop.uprn]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    props = {}
    for uprn in (A, B):
        r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": uprn})
        props[uprn] = db.get(User, r.json()["user_id"]).property
    assert props[A].round_key == props[B].round_key
    schedule.refresh_round(db, props[B])  # B's own answer puts the garden bin on the round
    assert _dates(db, props[A], "Garden Bin") == [WK1]
    calls.clear()
    return props, answers, calls


def _dates(db, prop, label):
    return sorted(e.date for e in db.scalars(select(CollectionEvent).where(
        CollectionEvent.round_key == prop.round_key, CollectionEvent.date >= TODAY))
        if e.bin_type.council_label == label)


def test_a_bin_only_next_door_has_is_refreshed_from_next_door(client, db, monkeypatch):
    props, answers, calls = _street(client, db, monkeypatch)
    answers[A] = [RawCollection("Refuse", WK1), RawCollection("Refuse", WK2)]
    answers[B] = answers[A] + [RawCollection("Garden Bin", WK1), RawCollection("Garden Bin", WK2)]
    res = schedule.refresh_round(db, props[A])
    assert calls == [A, B]
    assert _dates(db, props[A], "Garden Bin") == [WK1, WK2]
    assert not res.diff.get("removed")


def test_its_dates_are_left_alone_when_next_door_cannot_be_asked(client, db, monkeypatch):
    props, answers, calls = _street(client, db, monkeypatch)
    answers[A] = [RawCollection("Refuse", WK1), RawCollection("Refuse", WK2)]
    answers[B] = AdapterError("site down", retryable=True)
    res = schedule.refresh_round(db, props[A])
    assert _dates(db, props[A], "Garden Bin") == [WK1] and not res.diff.get("removed")


def test_it_goes_when_no_home_on_the_round_has_it_any_more(client, db, monkeypatch):
    props, answers, calls = _street(client, db, monkeypatch)
    answers[A] = answers[B] = [RawCollection("Refuse", WK1), RawCollection("Refuse", WK2)]
    res = schedule.refresh_round(db, props[A])
    assert _dates(db, props[A], "Garden Bin") == [] and len(res.diff["removed"]) == 1


def test_a_resident_waiting_after_a_slow_lookup_is_not_kept_longer(client, db, monkeypatch):
    props, answers, calls = _street(client, db, monkeypatch)
    monkeypatch.setattr(schedule, "COVER_AFTER_SECONDS", -1.0)
    answers[A] = [RawCollection("Refuse", WK1), RawCollection("Refuse", WK2)]
    schedule.refresh_round(db, props[A], waiting=True)
    assert calls == [A] and _dates(db, props[A], "Garden Bin") == [WK1]
