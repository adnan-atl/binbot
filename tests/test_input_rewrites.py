"""Library modules that read the street from their house-number argument get the street."""
from app.adapters import ukbcd
from app.adapters.base import LookupInput
from app.adapters.overrides import INPUT_REWRITES, street_in_house_number
from app.adapters.registry import AdapterSpec


def test_street_from_the_address():
    out = street_in_house_number(LookupInput(postcode="DY10 1AA", house_number="12",
                                             address_line="12, LEA STREET, KIDDERMINSTER, DY10 1AA"))
    assert out.house_number == "LEA STREET" and out.postcode == "DY10 1AA"


def test_wyre_forest_module_gets_the_street(monkeypatch):
    seen = []
    monkeypatch.setattr(ukbcd.UKBCDAdapter, "_fetch", lambda self, lookup: seen.append(lookup) or [])
    spec = AdapterSpec(module="WyreForestDistrictCouncil", gss_code="E07000239", name="Wyre Forest", url="")
    ukbcd.UKBCDAdapter(spec)._fetch_direct(
        LookupInput(postcode="DY10 1AA", house_number="12", address_line="12, Lea Street, Kidderminster"))
    assert seen[0].house_number == "Lea Street"
    assert "WyreForestDistrictCouncil" in INPUT_REWRITES
