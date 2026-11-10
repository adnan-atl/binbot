"""A council's maintenance page has no dates either: say "down", not "nothing at this address"
(Stroud, 10 Nov 2026), so the resident is told to try again rather than only add bins by hand."""
import httpx
import pytest

from app.adapters import ukbcd
from app.adapters.base import AdapterError, LookupInput, NoCollectionsForAddress, unreachable
from app.adapters.registry import AdapterSpec
from app.adapters.ukbcd import UKBCDAdapter, site_down

STROUD = AdapterSpec(module="StroudDistrictCouncil", gss_code="E07000082", name="Stroud",
                     url="https://www.stroud.gov.uk/my-house", needs_uprn=True)


def _page(monkeypatch, status, text):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(status, text=text))


def test_maintenance_page(monkeypatch):
    _page(monkeypatch, 200, "<h1>Planned maintenance in progress</h1>")
    assert site_down("https://www.stroud.gov.uk/my-house") == "maintenance"


def test_server_error(monkeypatch):
    _page(monkeypatch, 503, "")
    assert site_down("https://x.gov.uk") == "HTTP 503"


def test_normal_page_and_refusals_prove_nothing(monkeypatch):
    _page(monkeypatch, 200, "<h1>Your bin days</h1>")
    assert site_down("https://x.gov.uk") is None
    _page(monkeypatch, 403, "Forbidden")
    assert site_down("https://x.gov.uk") is None


def test_no_collections_on_a_down_site_becomes_unreachable(monkeypatch):
    def empty(self, lookup):
        raise NoCollectionsForAddress("adapter returned no collections")

    monkeypatch.setattr(UKBCDAdapter, "_fetch_once", empty)
    _page(monkeypatch, 503, "Service Unavailable")
    with pytest.raises(AdapterError) as e:
        UKBCDAdapter(STROUD)._fetch_direct(LookupInput(postcode="GL10 3BH", uprn="1"))
    assert not isinstance(e.value, NoCollectionsForAddress) and unreachable(str(e.value))


def test_no_collections_on_a_working_site_stays_no_collections(monkeypatch):
    def empty(self, lookup):
        raise NoCollectionsForAddress("adapter returned no collections")

    monkeypatch.setattr(UKBCDAdapter, "_fetch_once", empty)
    _page(monkeypatch, 200, "<h1>Your bin days</h1>")
    with pytest.raises(NoCollectionsForAddress):
        UKBCDAdapter(STROUD)._fetch_direct(LookupInput(postcode="GL10 3BH", uprn="1"))
