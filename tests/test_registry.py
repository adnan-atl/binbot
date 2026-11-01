"""Council routing: every council code points at the right scraper, and our own scrapers
for councils the library doesn't cover. No network."""
from datetime import date

import pytest

from app.adapters import overrides as ov
from app.adapters.base import LookupInput, MissingInput, NoCollectionsForAddress
from app.adapters.registry import audit, load_specs


def test_every_council_code_is_real_and_claimed_once():
    """Re-run on every library upgrade: a wrong LAD24CD in input.json silently sends a whole
    council to another council's scraper (Stroud got Gosport's, Rugby got North
    Warwickshire's). Fix new ones in registry.LAD_FIX."""
    assert audit() == []


def test_misfiled_councils_are_routed_to_their_own_scraper():
    specs = load_specs()
    assert specs["E07000082"].module == "StroudDistrictCouncil"
    assert specs["E07000088"].module == "GosportBoroughCouncil"
    assert specs["E07000220"].module == "RugbyBoroughCouncil"
    assert specs["E07000218"].module == "NorthWarwickshireBoroughCouncil"
    assert specs["E07000242"].module == "EastHertsCouncil"
    assert specs["E07000229"].module == "AdurAndWorthingCouncils"  # Worthing
    assert specs["E06000064"].module == "WestMorlandAndFurness"
    assert specs["E09000009"].module == "LondonBoroughEaling"  # not the deprecated one
    assert specs["E09000020"].module == "BinBotKensingtonAndChelsea"


ROW = ('<tr><td>{s}</td><td>{n}</td><td>{p}</td><td></td><td>{w}</td><td>{f}</td></tr>')


def _page(street, rows):
    body = "".join(ROW.format(s=street, n=n, p=p, w=w, f=f) for n, p, w, f in rows)
    return f"<table class='table'><thead><tr><th>x</th></tr></thead><tbody>{body}</tbody></table>"


CROMWELL = _page("Cromwell Road", [
    ("[1 - 159]", "Odd numbers only", "Tuesday, Friday", ""),
    ("[3-41]", "Odd numbers only", "Tuesday, Friday", "Tuesday"),
    ("[2 - 100]", "Even numbers only", "Monday, Thursday", ""),
    ("[226 - 100]", "Even numbers only", "Tuesday, Friday", ""),
])
PORTOBELLO = _page("Portobello Road", [
    ("[2 - 80a evens] & [9 - 63 odds]", "Both sides", "Monday, Thursday", "Monday"),
    ("[82 -  330 evens] & [65 - 373 odds]", "Both sides", "Monday, Tuesday, Wednesday", ""),
])
ELGIN = _page("Elgin Crescent", [("All", "Both sides", "Tuesday, Friday", "Friday")])


def test_rbkc_picks_the_row_for_the_house():
    rows = ov.rbkc_rows(CROMWELL)
    assert ov.rbkc_pick(rows, "64")["waste"] == ["Monday", "Thursday"]
    assert ov.rbkc_pick(rows, "150")["waste"] == ["Tuesday", "Friday"]  # reversed range
    assert ov.rbkc_pick(rows, "21")["food"] == ["Tuesday"]  # tighter stretch wins
    assert ov.rbkc_pick(rows, "Flat 3, 77")["food"] == []


def test_rbkc_per_range_parity_and_whole_street():
    rows = ov.rbkc_rows(PORTOBELLO)
    assert ov.rbkc_pick(rows, "40")["food"] == ["Monday"]
    assert ov.rbkc_pick(rows, "41")["waste"] == ["Monday", "Thursday"]
    assert ov.rbkc_pick(rows, "101")["waste"] == ["Monday", "Tuesday", "Wednesday"]
    assert ov.rbkc_pick(ov.rbkc_rows(ELGIN), None)["food"] == ["Friday"]


def test_rbkc_needs_a_number_only_when_the_street_is_split():
    with pytest.raises(MissingInput):
        ov.rbkc_pick(ov.rbkc_rows(CROMWELL), None)
    with pytest.raises(NoCollectionsForAddress):
        ov.rbkc_pick(ov.rbkc_rows(CROMWELL), "999")


def test_rbkc_twice_weekly_becomes_two_weekly_series_per_bin():
    today = date(2026, 11, 2)  # Monday
    items = ov.rbkc_collections(ov.rbkc_pick(ov.rbkc_rows(ELGIN), "4"), today)
    general = sorted(i.date for i in items if i.label == "General waste")
    assert general[:2] == [date(2026, 11, 3), date(2026, 11, 6)]  # Tue, Fri
    assert {i.label for i in items} == {"General waste", "Recycling", "Food waste"}


def test_rbkc_street_comes_from_the_address():
    streets = ["High Street", "Kensington High Street", "Abingdon Road", "Abingdon Gardens"]
    assert ov.rbkc_street_in("FLAT 2, 120 KENSINGTON HIGH STREET, LONDON, W8 7RL", streets) \
        == "Kensington High Street"
    assert ov.rbkc_street_in("10 Abingdon Road, London", streets) == "Abingdon Road"
    assert ov.rbkc_street_in("1 Nowhere Lane", streets) is None
    lk = LookupInput(postcode="W8 6AH", house_number="10", address_line="10 Abingdon Road")
    assert ov._rbkc_street(lk, streets) == "Abingdon Road"


# Captured from rbkc.gov.uk on 1 Nov 2026 (Ladbroke Grove), whitespace collapsed.
LADBROKE_LIVE = (
    '<table class="table"> <thead> <tr> <th>Street name</th> <th>Property numbers</th> '
    '<th>Number details</th> <th>Further details</th> <th>General waste and recycling '
    'collections</th> <th>Food waste collection</th> </tr> </thead> <tbody> '
    '<tr> <td class="table__cell">Ladbroke Grove </td> <td class="table__cell">[9 - 93]</td> '
    '<td class="table__cell">Odd numbers only</td> <td class="table__cell">West Side From '
    'Holland Park Avenue To Blenheim Crescent</td> <td class="table__cell">Monday, Thursday</td> '
    '<td class="table__cell"></td> </tr> '
    '<tr> <td class="table__cell">Ladbroke Grove </td> <td class="table__cell">[70 - 150]</td> '
    '<td class="table__cell">Even numbers only</td> <td class="table__cell">East Side From '
    'Arundel Gardens To Cambridge Gardens</td> <td class="table__cell">Tuesday, Friday</td> '
    '<td class="table__cell">Friday</td> </tr> </tbody> </table>')


def test_rbkc_live_page_shape():
    rows = ov.rbkc_rows(LADBROKE_LIVE)
    assert ov.rbkc_pick(rows, "11")["waste"] == ["Monday", "Thursday"]
    assert ov.rbkc_pick(rows, "100") == {"numbers": "[70 - 150]", "parity": "even numbers only",
                                         "details": "East Side From Arundel Gardens To Cambridge Gardens",
                                         "waste": ["Tuesday", "Friday"], "food": ["Friday"]}
