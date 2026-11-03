"""The scraper container boundary: api/worker -> HTTP -> scraper service -> scraper."""
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.base import AdapterError, LookupInput, MissingInput, NoCollectionsForAddress, RawCollection
from app.adapters.registry import AdapterSpec
from app.adapters.ukbcd import UKBCDAdapter
from app.core.config import get_settings
from app.scraper import service
from app.scraper.wire import spec_from_json, spec_to_json

SPEC = AdapterSpec(module="SomeCouncil", gss_code="E09000001", name="Some", url="https://x/XXXXXXXX",
                   needs_uprn=True, placeholder="XXXXXXXX", requires=frozenset({"uprn"}))
HOME = LookupInput(postcode="E8 3PB", uprn="100023", house_number="12")


@pytest.fixture
def remote(monkeypatch):
    """Point the adapter at the service, with httpx.post going to the in-process app."""
    s = get_settings()
    monkeypatch.setattr(s, "scraper_url", "http://scraper:8100")
    monkeypatch.setattr(s, "scraper_token", "t0k")
    client = TestClient(service.app)

    def post(url, json, headers, timeout):
        return client.post(url.replace("http://scraper:8100", ""), json=json, headers=headers)
    monkeypatch.setattr(httpx, "post", post)
    calls = []

    def run(result):
        def fake(self, lookup):
            calls.append((self.spec, lookup))
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(UKBCDAdapter, "fetch_local", fake)
        return calls
    return run


def test_spec_round_trips():
    assert spec_from_json(spec_to_json(SPEC)) == SPEC


def test_collections_come_back(remote):
    calls = remote([RawCollection("Recycling", date(2026, 11, 10))])
    assert UKBCDAdapter(SPEC).fetch(HOME) == [RawCollection("Recycling", date(2026, 11, 10))]
    assert calls == [(SPEC, HOME)]  # the service rebuilt the same spec and home


@pytest.mark.parametrize("err", [MissingInput("house_number"), NoCollectionsForAddress("communal bins"),
                                 AdapterError("site changed", retryable=False), AdapterError("timeout")])
def test_errors_keep_their_type(remote, err):
    remote(err)
    with pytest.raises(type(err)) as got:
        UKBCDAdapter(SPEC).fetch(HOME)
    assert got.value.retryable == err.retryable
    if isinstance(err, MissingInput):
        assert got.value.field == "house_number"


def test_crash_is_an_adapter_error(remote):
    remote(RuntimeError("boom"))
    with pytest.raises(AdapterError, match="boom"):
        UKBCDAdapter(SPEC).fetch(HOME)


def test_bad_token_refused(remote, monkeypatch):
    remote([])
    r = TestClient(service.app).post("/fetch", json={}, headers={"X-Scraper-Token": "nope"})
    assert r.status_code == 403


def test_service_down_is_retryable(monkeypatch):
    monkeypatch.setattr(get_settings(), "scraper_url", "http://scraper:8100")

    def down(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(AdapterError, match="unavailable") as got:
        UKBCDAdapter(SPEC).fetch(HOME)
    assert got.value.retryable


def test_validation_stays_on_our_side(monkeypatch):
    monkeypatch.setattr(get_settings(), "scraper_url", "http://scraper:8100")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("should not call the scraper"))
    with pytest.raises(MissingInput):
        UKBCDAdapter(SPEC).fetch(LookupInput(postcode="E8 3PB"))
