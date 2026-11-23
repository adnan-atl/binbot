"""A background address lookup (prefetching for a neighbour's calendar) that times out doesn't
page the team: nobody is waiting on it. A resident's own lookup failing still does."""
from types import SimpleNamespace

import httpx

from app.services import geo


def _settings():
    return SimpleNamespace(address_provider="os_places", address_fallback="none",
                           os_places_api_key="", os_places_per_minute=45)


def test_background_timeout_sends_no_alert(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.inbox.alert_address_lookup_failed", lambda pc, e: sent.append(pc))
    def boom(s, pc):
        raise httpx.ReadTimeout("The read operation timed out")
    monkeypatch.setitem(geo._PROVIDERS, "os_places", boom)
    assert geo._fetch_addresses(_settings(), "SW16 1LP", paid_ok=False, background=True) == []
    assert sent == []
    assert geo._fetch_addresses(_settings(), "SW16 1LP") == []
    assert sent == ["SW16 1LP"]
