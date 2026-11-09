"""OS Places allows 50 lookups a minute for everything: sign-ups first, background work
(the neighbour prefetch) only while the minute is under half the budget."""
from types import SimpleNamespace

from app.services import geo


def _settings(per_minute=4):
    return SimpleNamespace(demo_mode=False, address_provider="os_places", address_fallback="ideal_postcodes",
                           os_places_api_key="os", ideal_postcodes_api_key="ip",
                           os_places_per_minute=per_minute)


def _fresh(monkeypatch, per_minute=4):
    geo._os_calls.clear()
    geo._cache.clear()
    monkeypatch.setattr(geo, "get_settings", lambda: _settings(per_minute))
    calls = []
    monkeypatch.setattr(geo, "_PROVIDERS", {
        "os_places": lambda s, pc: calls.append(("os", pc)) or [geo.AddressHit(uprn="1", line="1 A St", house_number="1")],
        "ideal_postcodes": lambda s, pc: calls.append(("paid", pc)) or [geo.AddressHit(uprn="2", line="2 A St", house_number="2")]})
    return calls


def test_background_stops_at_half_the_budget(monkeypatch):
    calls = _fresh(monkeypatch)
    for i in range(3):
        geo.lookup_addresses(f"E8 {i}AA", paid_ok=False)
    assert [c[0] for c in calls] == ["os", "os"]  # third skipped: never paid, never waits


def test_sign_ups_get_the_rest_then_fall_back(monkeypatch):
    calls = _fresh(monkeypatch)
    for i in range(2):
        geo.lookup_addresses(f"E8 {i}AA", paid_ok=False)
    assert geo.os_slot(wait=0) and geo.os_slot(wait=0)   # sign-ups still have room
    assert not geo.os_slot(wait=0)                        # minute used up
    hits = geo._fetch_addresses(_settings(), "E9 9ZZ", alert=False)
    assert calls[-1] == ("paid", "E9 9ZZ") and hits[0].uprn == "2"


def test_old_calls_leave_the_window(monkeypatch):
    _fresh(monkeypatch, per_minute=2)
    assert geo.os_slot(wait=0) and geo.os_slot(wait=0) and not geo.os_slot(wait=0)
    geo._os_calls[0] -= 61
    geo._os_calls[1] -= 61
    assert geo.os_slot(wait=0)
    geo._os_calls.clear()
