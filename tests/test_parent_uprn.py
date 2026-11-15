"""A flat whose council keeps the communal bins against the building: "not found" for the
flat's UPRN, the collections under the block's (Ealing, W3, 15 Nov 2026). Ask again with the
building's UPRN, which the address list gives us for free."""
from datetime import date

import pytest

from app.adapters.base import LookupInput, NoCollectionsForAddress, RawCollection
from app.adapters.registry import AdapterSpec
from app.adapters.ukbcd import UKBCDAdapter
from app.services import geo
from app.scraper import wire

FLAT, BLOCK = "12197123", "12197100"


def _adapter(monkeypatch, requires=frozenset({"uprn"})):
    spec = AdapterSpec(module="LondonBoroughEaling", gss_code="E09000009", name="Ealing", url="",
                       needs_uprn=True, requires=requires)
    tried = []

    def once(self, lookup):
        tried.append(lookup.uprn)
        if lookup.uprn != BLOCK:
            raise NoCollectionsForAddress("adapter returned no collections")
        return [RawCollection("Refuse", date(2026, 11, 17))]

    monkeypatch.setattr(UKBCDAdapter, "_fetch_once", once)
    return UKBCDAdapter(spec), tried


def test_a_flat_with_nothing_is_tried_as_its_building(monkeypatch):
    a, tried = _adapter(monkeypatch)
    got = a._fetch_with_retry(LookupInput(postcode="W3 8NE", uprn=FLAT, parent_uprn=BLOCK))
    assert got[0].label == "Refuse" and tried == [FLAT, BLOCK]


def test_without_a_building_it_still_fails(monkeypatch):
    a, tried = _adapter(monkeypatch)
    with pytest.raises(NoCollectionsForAddress):
        a._fetch_with_retry(LookupInput(postcode="W3 8NE", uprn=FLAT))
    assert tried == [FLAT]


def test_councils_that_dont_look_up_by_uprn_are_not_retried(monkeypatch):
    a, tried = _adapter(monkeypatch, requires=frozenset({"postcode"}))
    with pytest.raises(NoCollectionsForAddress):
        a._fetch_with_retry(LookupInput(postcode="W3 8NE", uprn=FLAT, parent_uprn=BLOCK))
    assert tried == [FLAT]


def test_the_building_reaches_the_scraper_container():
    lookup = LookupInput(postcode="W3 8NE", uprn=FLAT, parent_uprn=BLOCK)
    assert wire.lookup_from_json(wire.lookup_to_json(lookup)).parent_uprn == BLOCK


def test_the_building_comes_from_the_cached_address_list_only(monkeypatch):
    monkeypatch.setitem(geo._cache, ("addr", "W3 8NE"), (9e12, [
        geo.AddressHit(uprn=FLAT, line="25 Southsea Heights", house_number="25", parent_uprn=BLOCK)]))
    assert geo.cached_parent_uprn("w38ne", FLAT) == BLOCK
    assert geo.cached_parent_uprn("W3 8NE", "999") is None
    assert geo.cached_parent_uprn("E8 3PB", FLAT) is None  # not cached: no lookup made
