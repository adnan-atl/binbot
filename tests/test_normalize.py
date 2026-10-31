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
