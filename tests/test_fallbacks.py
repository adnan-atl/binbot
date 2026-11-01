"""Last-resort fallback: a round that had a calendar keeps its pattern when the council
goes quiet. No network."""
from datetime import date, timedelta

from sqlalchemy import select

from app.adapters.base import AdapterError, RawCollection
from app.models import ChangeLog, CollectionEvent, User
from app.services import schedule


def _signup(client, db, monkeypatch, items):
    state = {"items": items, "error": None}

    def fetch(council, prop):
        if state["error"]:
            raise state["error"]
        return state["items"]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    uid = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()["user_id"]
    return db.get(User, uid).property, state


def test_council_outage_projects_the_pattern_forward(client, db, monkeypatch):
    """Fortnightly bin, known dates end in 10 days, council's site goes down: carry the
    pattern on for a few weeks instead of letting reminders stop."""
    today = date.today()
    first = today + timedelta(days=(1 - today.weekday()) % 7 or 7) - timedelta(weeks=4)
    dates = [first + timedelta(weeks=2 * i) for i in range(3)]  # 4 weeks ago .. this fortnight
    prop, state = _signup(client, db, monkeypatch, [RawCollection("Refuse", d) for d in dates])
    # The signup fetch keeps only future dates; put the history back like weekly refreshes do.
    bt = db.scalar(select(CollectionEvent.bin_type_id))
    for d in dates:
        if d < today:
            db.add(CollectionEvent(round_key=prop.round_key, bin_type_id=bt, date=d))
    db.commit()

    state["error"] = AdapterError("site down", retryable=True)
    res = schedule.refresh_round(db, prop)
    assert res.error
    future = sorted(e.date for e in db.scalars(select(CollectionEvent).where(
        CollectionEvent.round_key == prop.round_key, CollectionEvent.date >= today)))
    assert future[-1] > dates[-1] and future[-1] > today + timedelta(days=14)
    assert all((b - a).days == 14 for a, b in zip(future, future[1:]))
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "projected"))

    # Council's back: its dates replace the projection, and nothing is reported as removed.
    state["error"] = None
    state["items"] = [RawCollection("Refuse", d) for d in future[:1]]
    res = schedule.refresh_round(db, prop)
    assert not res.diff.get("removed")


def test_no_projection_without_a_pattern(client, db, monkeypatch):
    today = date.today()
    prop, state = _signup(client, db, monkeypatch, [RawCollection("Refuse", today + timedelta(days=3))])
    state["error"] = AdapterError("site down", retryable=True)
    schedule.refresh_round(db, prop)
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "projected")) is None


def test_daily_collections_keep_their_days():
    """Regression (Westminster, NW1 5AY): rubbish goes out every day. 14 dates over two
    weeks used to come out as no day at all, so the resident saw no rubbish day."""
    from app.services.normalize import describe_rule

    mon = date(2026, 11, 2)
    daily = describe_rule([mon + timedelta(days=i) for i in range(14)])
    assert daily["days"] == ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    assert daily["frequency"] == "weekly"
    weekdays = describe_rule([mon + timedelta(days=i) for i in range(14) if (mon + timedelta(days=i)).weekday() < 5])
    assert weekdays["days"] == ["MON", "TUE", "WED", "THU", "FRI"]
    # Still filters a one-off bank holiday shift on a weekly round.
    tue = date(2026, 11, 3)
    shifted = [tue + timedelta(weeks=i) for i in range(8)]
    shifted[5] += timedelta(days=1)
    assert describe_rule(shifted)["days"] == ["TUE"]


def test_calendar_without_general_waste_is_a_gap_to_fill():
    """A scrape that returns recycling but no rubbish is partial: the checker flags it, so
    the verify step asks the neighbours before the resident sees a calendar without it."""
    from app.services import checks

    found = checks.run([{"category": "recycling", "label": "Recycling", "days": ["TUE"],
                         "history": []}], "Westminster", "E09000033")
    assert any(c["kind"] == "missing_stream" and c["category"] == "general" for c in found)


def _addr_settings(**kw):
    from types import SimpleNamespace

    base = dict(demo_mode=False, address_provider="os_places", address_fallback="ideal_postcodes",
                os_places_api_key="os", ideal_postcodes_api_key="ip")
    return SimpleNamespace(**{**base, **kw})


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def raise_for_status(self):
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._body


IDEAL = {"result": [{"uprn": "100023336956", "line_1": "Flat 3", "line_2": "18 Elm Road",
                     "post_town": "London", "postcode": "E8 3PB", "building_number": "18",
                     "sub_building_name": "Flat 3"}]}


def test_address_lookup_falls_back_when_os_places_is_down(monkeypatch):
    from app.services import geo

    geo._cache.clear()
    calls = []

    def get(url, **kw):
        calls.append(url)
        return _Resp(503, {}) if "os.uk" in url else _Resp(200, IDEAL)

    monkeypatch.setattr(geo.httpx, "get", get)
    monkeypatch.setattr(geo, "get_settings", lambda: _addr_settings())
    hits = geo.lookup_addresses("E8 3PB")
    assert [h.uprn for h in hits] == ["100023336956"] and hits[0].house_number == "18"
    assert "Flat 3, 18 Elm Road, London, E8 3PB" == hits[0].line
    assert ["os.uk" in c for c in calls] == [True, False]
    geo._cache.clear()


def test_address_fallback_only_when_needed_and_never_crashes(monkeypatch):
    from app.services import geo

    geo._cache.clear()
    calls = []
    os_ok = {"results": [{"DPA": {"UPRN": 1, "BUILDING_NUMBER": "92", "ADDRESS": "92, X ROAD, E8 3PB"}}]}

    def get(url, **kw):
        calls.append(url)
        return _Resp(200, os_ok) if "os.uk" in url else _Resp(200, IDEAL)

    monkeypatch.setattr(geo.httpx, "get", get)
    monkeypatch.setattr(geo, "get_settings", lambda: _addr_settings())
    assert geo.lookup_addresses("E8 3PB")[0].uprn == "1"
    assert len(calls) == 1  # OS answered: the paid fallback isn't called
    geo._cache.clear()

    # Both down: no exception, an empty list (the resident types a house number instead).
    monkeypatch.setattr(geo.httpx, "get", lambda url, **kw: _Resp(500, {}))
    assert geo.lookup_addresses("E8 3PB") == []
    geo._cache.clear()
