"""'26 OLD MUSTARD MILL' as the house number: councils that match on the number alone find
nothing (Norwich, 10 Nov 2026). Retry with plainer forms before giving up."""
from datetime import date

import pytest

from app.adapters.base import AdapterError, LookupInput, RawCollection
from app.adapters.registry import AdapterSpec
from app.adapters.ukbcd import UKBCDAdapter, paon_alternatives


def test_alternatives():
    assert paon_alternatives("26 OLD MUSTARD MILL") == ["26", "OLD MUSTARD MILL"]
    assert paon_alternatives("Flat 3, 18") == ["3", "18"]
    assert paon_alternatives("12A") == []
    assert paon_alternatives("12") == []
    assert paon_alternatives("Rose Cottage") == []
    assert paon_alternatives(None) == []


def _adapter(monkeypatch, fail_unless, browser=False):
    spec = AdapterSpec(module="NorwichCityCouncil", gss_code="E07000148", name="Norwich", url="",
                       needs_postcode=True, needs_house_number=True, needs_browser=browser)
    tried = []

    def once(self, lookup):
        tried.append(lookup.house_number)
        if lookup.house_number != fail_unless:
            raise AdapterError("NorwichCityCouncil: AttributeError: 'NoneType' object has no attribute 'find'")
        return [RawCollection("Refuse", date(2026, 11, 17))]

    monkeypatch.setattr(UKBCDAdapter, "_fetch_once", once)
    return UKBCDAdapter(spec), tried


def test_retries_with_the_number(monkeypatch):
    a, tried = _adapter(monkeypatch, "26")
    got = a._fetch_direct(LookupInput(postcode="NR1 2GE", house_number="26 OLD MUSTARD MILL"))
    assert got[0].label == "Refuse" and tried == ["26 OLD MUSTARD MILL", "26"]


def test_gives_up_with_the_first_error(monkeypatch):
    a, tried = _adapter(monkeypatch, "never")
    with pytest.raises(AdapterError, match="NoneType"):
        a._fetch_direct(LookupInput(postcode="NR1 2GE", house_number="26 OLD MUSTARD MILL"))
    assert tried == ["26 OLD MUSTARD MILL", "26", "OLD MUSTARD MILL"]


def test_browser_scrapers_get_one_retry(monkeypatch):
    a, tried = _adapter(monkeypatch, "never", browser=True)
    with pytest.raises(AdapterError):
        a._fetch_direct(LookupInput(postcode="NR1 2GE", house_number="26 OLD MUSTARD MILL"))
    assert len(tried) == 2


def test_no_retry_when_the_site_is_down(monkeypatch):
    spec = AdapterSpec(module="X", gss_code="E0", name="X", url="", needs_house_number=True)
    tried = []

    def once(self, lookup):
        tried.append(lookup.house_number)
        raise AdapterError("X: timed out after 60s")

    monkeypatch.setattr(UKBCDAdapter, "_fetch_once", once)
    with pytest.raises(AdapterError):
        UKBCDAdapter(spec)._fetch_direct(LookupInput(postcode="NR1 2GE", house_number="26 OLD MUSTARD MILL"))
    assert tried == ["26 OLD MUSTARD MILL"]
