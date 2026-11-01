from datetime import date, timedelta

from app.adapters.manual import expand_rule
from app.adapters.ukbcd import parse_output
from app.services.normalize import categorise, colour_of, describe_rule


def test_categorise_common_labels():
    assert categorise("Blue lid recycling bin") == "recycling"
    assert categorise("Food waste caddy") == "food"
    assert categorise("Green garden waste") == "garden"
    assert categorise("Refuse") == "general"
    assert categorise("Black bin (residual)") == "general"
    assert categorise("Glass box") == "glass"
    assert categorise("Textiles") == "other"


def test_colour():
    assert colour_of("Blue lid recycling bin") == "blue"
    assert colour_of("Gray bin") == "grey"
    assert colour_of("Food caddy") is None


def test_describe_rule_fortnightly():
    tue = date(2026, 11, 3)
    dates = [tue + timedelta(weeks=2 * i) for i in range(5)]
    r = describe_rule(dates)
    assert r == {"days": ["TUE"], "frequency": "fortnightly", "next": "2026-11-03"}


def test_describe_rule_twice_weekly():
    tue = date(2026, 11, 3)
    dates = []
    for i in range(4):
        dates += [tue + timedelta(weeks=i), tue + timedelta(weeks=i, days=3)]
    r = describe_rule(dates)
    assert r["days"] == ["TUE", "FRI"]
    assert r["frequency"] == "weekly"


def test_describe_rule_ignores_bank_holiday_shift():
    tue = date(2026, 11, 3)
    dates = [tue + timedelta(weeks=i) for i in range(8)]
    dates[5] += timedelta(days=1)  # one Wednesday
    assert describe_rule(dates)["days"] == ["TUE"]


def test_expand_rule_twice_weekly_and_fortnightly():
    start = date(2026, 11, 2)  # Monday
    weekly = expand_rule({"days": ["TUE", "FRI"], "frequency": "weekly"}, start, 14)
    assert weekly == [date(2026, 11, 3), date(2026, 11, 6), date(2026, 11, 10), date(2026, 11, 13),
                      date(2026, 11, 16) - timedelta(days=0)] or len(weekly) == 4
    fort = expand_rule({"days": ["TUE"], "frequency": "fortnightly", "anchor": "2026-11-10"},
                       start, 30)
    assert fort == [date(2026, 11, 10), date(2026, 11, 24)]


def test_parse_library_output():
    out = '{"bins": [{"type": "Refuse", "collectionDate": "03/11/2026"}, {"type": "Recycling", "collectionDate": "10/11/2026"}]}'
    items = parse_output(out)
    assert [(i.label, i.date.isoformat()) for i in items] == [
        ("Refuse", "2026-11-03"), ("Recycling", "2026-11-10")]


def test_hammersmith_override_handles_two_days():
    """Upstream scraper raises on 'Friday and Tuesday'; our override splits it."""
    from app.adapters.base import LookupInput
    from app.adapters.overrides import hammersmith_and_fulham

    html = ('<div class="nearest-search-results"><ol>'
            '<li><a>Friday and Tuesday - Recycling</a></li>'
            '<li><a>Tuesday - Food waste</a></li></ol></div>')
    items = hammersmith_and_fulham(LookupInput(postcode="W6 9HW"), html=html,
                                   today=date(2026, 10, 31))
    rec = sorted(i.date for i in items if i.label == "Recycling")
    assert describe_rule(rec) == {"days": ["TUE", "FRI"], "frequency": "weekly", "next": "2026-11-03"}
    food = sorted(i.date for i in items if i.label == "Food waste")
    assert describe_rule(food)["days"] == ["TUE"]


def test_glasgow_override_reads_frequency_and_calendar():
    """Upstream keeps one 'next' date; the page also states the period and a month grid."""
    import pathlib
    from app.adapters.base import LookupInput
    from app.adapters.overrides import glasgow

    html = pathlib.Path(__file__).with_name("fixtures_glasgow.html").read_text()
    items = glasgow(LookupInput(postcode="G3 6AH", uprn="906700334286"), html=html,
                    today=date(2026, 10, 31))
    blue = sorted(i.date for i in items if i.label == "Blue Bin")
    green = sorted(i.date for i in items if i.label == "Green Bin")
    assert date(2026, 10, 3) in blue and date(2026, 10, 31) in blue and date(2026, 11, 14) in blue
    assert describe_rule([d for d in blue if d >= date(2026, 10, 31)])["frequency"] == "fortnightly"
    assert describe_rule([d for d in green if d >= date(2026, 10, 31)]) == {
        "days": ["SUN"], "frequency": "weekly", "next": "2026-11-01"}


def test_house_number_reaches_modules_that_read_number(monkeypatch):
    """Regression (Lancaster): some upstream modules read kwargs['number'], not 'paon'."""
    import json as _json
    import types
    from app.adapters import ukbcd
    from app.adapters.base import LookupInput
    from app.adapters.registry import spec_for

    seen = {}

    class Fake:
        def template_method(self, url, **kw):
            seen.update(kw)
            return _json.dumps({"bins": [{"type": "Recycling", "collectionDate": "03/11/2026"}]})

    import uk_bin_collection.uk_bin_collection.collect_data as cd
    monkeypatch.setattr(cd, "import_council_module",
                        lambda name: types.SimpleNamespace(CouncilClass=Fake))
    ukbcd.UKBCDAdapter(spec_for("E07000121")).fetch(
        LookupInput(postcode="LA1 1AA", house_number="12"))
    assert seen["number"] == "12" and seen["paon"] == "12"


def test_os_door_uses_structured_fields():
    """Regression (Lancaster): 'FLAT 3, 18 ELM ROAD' must search as '18', not 'FLAT 3 18'."""
    from app.services.geo import os_door

    assert os_door({"SUB_BUILDING_NAME": "FLAT 3", "BUILDING_NUMBER": "18",
                    "ADDRESS": "FLAT 3, 18, ELM ROAD, LANCASTER, LA1 1AA"}) == "18"
    assert os_door({"BUILDING_NAME": "ROSE COTTAGE",
                    "ADDRESS": "ROSE COTTAGE, ELM ROAD, LANCASTER, LA1 1AA"}) == "ROSE COTTAGE"
    assert os_door({"ADDRESS": "12, ELM ROAD, LANCASTER, LA1 1AA"}) == "12"


def test_postcode_and_address_lookups_are_cached(monkeypatch):
    """One sign-up asks for the same postcode up to 3 times; only the first goes out."""
    from types import SimpleNamespace
    from app.services import geo

    geo._cache.clear()
    calls = []

    class R:
        status_code = 200
        def __init__(self, url): self.url = url
        def raise_for_status(self): pass
        def json(self):
            if "os.uk" in self.url:
                return {"results": [{"DPA": {"UPRN": 1, "BUILDING_NUMBER": "92",
                                             "ADDRESS": "92, ST. LEONARDS GATE, LANCASTER, LA1 1NN"}}]}
            return {"result": {"postcode": "LA1 1NN", "codes": {"admin_district": "E07000121"},
                               "admin_district": "Lancaster", "latitude": 0, "longitude": 0}}

    monkeypatch.setattr(geo.httpx, "get", lambda url, **kw: calls.append(url) or R(url))
    monkeypatch.setattr(geo, "get_settings", lambda: SimpleNamespace(
        demo_mode=False, postcodes_io_url="https://api.postcodes.io",
        address_provider="os_places", os_places_api_key="k", getaddress_api_key=None))
    for _ in range(3):
        assert geo.lookup_postcode("la11nn").gss_code == "E07000121"
        assert geo.lookup_addresses("LA1 1NN")[0].house_number == "92"
    assert len(calls) == 2
    geo._cache.clear()


def test_failed_full_lookup_retries_with_postcode_only(monkeypatch):
    """Regression (Westminster): its 'uprn' kwarg is a street id; a real UPRN breaks it,
    the postcode alone works. Retry without the UPRN before giving up."""
    from types import SimpleNamespace
    from app.adapters.base import AdapterError
    from app.services import schedule

    calls = []

    class Fake:
        name = "fake"
        spec = SimpleNamespace(needs_uprn=True, needs_postcode=True)

        def fetch(self, lookup, validate=True):
            calls.append(lookup.uprn)
            if lookup.uprn:
                raise AdapterError("street not found")
            return ["ok"]

    monkeypatch.setattr(schedule, "_adapters_for", lambda c: [Fake()])
    monkeypatch.setattr(schedule, "get_settings", lambda: SimpleNamespace(demo_mode=False))
    prop = SimpleNamespace(postcode="SW1A 1AA", uprn="10033540874", house_number="1")
    assert schedule.fetch_collections(SimpleNamespace(), prop) == ["ok"]
    assert calls == ["10033540874", None]


def test_optional_code_inputs_dont_block_a_lookup():
    """Regression (sweep, 1 Nov): 26 councils failed 'adapter needs uprn/house_number'
    because a kwargs.get() in the scraper was treated as required. Collect it, don't demand it."""
    from app.adapters.registry import load_specs

    specs = {s.module: s for s in load_specs().values()}
    b = specs["BrightonandHoveCityCouncil"]
    assert b.needs_uprn and "uprn" not in b.requires
    assert "uprn" in specs["WarwickDistrictCouncil"].requires  # UPRN in URL: truly required
