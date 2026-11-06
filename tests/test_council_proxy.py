"""Councils whose sites don't answer our server get one more try through the UK proxy."""
from datetime import date

import pytest

from app.adapters import ukbcd
from app.adapters.base import AdapterError, LookupInput, RawCollection
from app.adapters.registry import AdapterSpec
from app.core.config import get_settings

SPEC = AdapterSpec(module="SlowCouncil", gss_code="X1", name="Slow", url="https://x")
HOME = LookupInput(postcode="G3 6AA", uprn="1")


@pytest.fixture
def proxied(monkeypatch):
    monkeypatch.setenv("COUNCIL_PROXY_URL", "http://user:pass@proxy:8000")
    get_settings.cache_clear()
    ukbcd._NEEDS_PROXY.clear()
    calls = []
    monkeypatch.setattr(ukbcd, "_fetch_via_proxy",
                        lambda spec, lookup, proxy: calls.append(proxy) or [RawCollection("Blue bin", date(2026, 11, 9))])
    yield calls
    get_settings.cache_clear()


def test_a_site_that_doesnt_answer_is_tried_through_the_proxy_then_remembered(proxied, monkeypatch):
    direct = []
    def blocked(self, lookup):
        direct.append(1)
        raise AdapterError("glasgow.gov.uk: The read operation timed out")
    monkeypatch.setattr(ukbcd.UKBCDAdapter, "_fetch_direct", blocked)
    assert ukbcd.UKBCDAdapter(SPEC).fetch_local(HOME)[0].label == "Blue bin"
    assert proxied == ["http://user:pass@proxy:8000"] and len(direct) == 1
    ukbcd.UKBCDAdapter(SPEC).fetch_local(HOME)  # next time: straight to the proxy
    assert len(direct) == 1 and len(proxied) == 2


def test_other_failures_dont_use_the_proxy(proxied, monkeypatch):
    def broken(self, lookup):
        raise AdapterError("adapter returned no collections")
    monkeypatch.setattr(ukbcd.UKBCDAdapter, "_fetch_direct", broken)
    with pytest.raises(AdapterError):
        ukbcd.UKBCDAdapter(SPEC).fetch_local(HOME)
    assert proxied == []


def test_no_proxy_configured_just_fails(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setattr(ukbcd.UKBCDAdapter, "_fetch_direct",
                        lambda self, lookup: (_ for _ in ()).throw(AdapterError("timed out")))
    with pytest.raises(AdapterError):
        ukbcd.UKBCDAdapter(SPEC).fetch_local(HOME)
