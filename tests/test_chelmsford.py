"""Chelmsford: the round from the address search, dates read from the round's calendar page
(the .ics file the library looked for is gone). Pages follow chelmsford.gov.uk, Nov 2026."""
from datetime import date

import httpx
import pytest

from app.adapters.base import LookupInput, NoCollectionsForAddress
from app.adapters.overrides import chelmsford, chelmsford_calendar, chelmsford_round

BASE = "https://www.chelmsford.gov.uk/bins-and-recycling/check-your-collection-day/"


def row(address, rnd):
    return (f"<tr><td>{address}</td><td><p>This property is covered by the {rnd} collection round.</p>"
            f'<a href="{BASE}{rnd.lower().replace(" ", "-")}-collection-calendar/">calendar</a></td></tr>')


SEARCH = ('<input id="14532_keyword" name="14532_keyword">'
          '<table class="table directories-table__table"><tr><th>Address</th><th>Waste</th></tr>'
          + row("1 Celeborn Street, South Woodham Ferrers, Chelmsford, CM3 7AE", "Tuesday B")
          + row("11 Celeborn Street, South Woodham Ferrers, Chelmsford, CM3 7AE", "Tuesday B")
          + row("1 Other Road, South Woodham Ferrers, Chelmsford, CM3 7AE", "Friday A") + "</table>")
CAL = ("<h1>Tuesday B kerbside collection calendar</h1><h2>November 2026</h2><ul>"
       "<li><p>Tuesday 3 November: food waste, brown bin, paper sack, plastic and cartons bag</p></li>"
       "<li><p>Tuesday 10 November: food waste, black bin, green box, card sack</p></li>"
       "<li><p>Tuesday 17 November: food waste, brown bin, paper sack, plastic and cartons bag</p></li></ul>"
       "<h2>Also in this section</h2><ul><li>Missed collections</li></ul>")


def test_round_for_the_home():
    line = "11, CELEBORN STREET, SOUTH WOODHAM FERRERS, CHELMSFORD, CM3 7AE"
    assert chelmsford_round(SEARCH, "11", line).endswith("tuesday-b-collection-calendar/")
    # "1" is on two streets with different rounds: the street in the address decides
    assert chelmsford_round(SEARCH, "1", "1, OTHER ROAD, SOUTH WOODHAM FERRERS").endswith("friday-a-collection-calendar/")
    assert chelmsford_round(SEARCH, "1", None) is None      # can't tell which street: don't guess
    assert chelmsford_round(SEARCH, "99", None) is None     # rounds differ and the home isn't listed


def test_calendar_dates_and_names():
    items = chelmsford_calendar(CAL, date(2026, 11, 9))
    on = {}
    for c in items:
        on.setdefault(c.date, []).append(c.label)
    assert on[date(2026, 11, 10)] == ["Food waste", "Black bin", "Green box (glass and cans)", "Card sack"]
    assert on[date(2026, 11, 17)] == ["Food waste", "Brown bin", "Paper sack", "Plastic and cartons bag"]
    assert len(on) == 3  # "Missed collections" in the footer list isn't a date


def test_end_to_end_keeps_only_upcoming_dates():
    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        if "collection-calendar" in req.url.path:
            return httpx.Response(200, text=CAL)
        return httpx.Response(200, text=SEARCH)

    items = chelmsford(LookupInput(postcode="CM3 7AE", house_number="11",
                                   address_line="11, CELEBORN STREET, SOUTH WOODHAM FERRERS, CHELMSFORD, CM3 7AE"),
                       client=httpx.Client(transport=httpx.MockTransport(handler)), today=date(2026, 11, 9))
    assert "14532_keyword=CM3" in seen[1] and seen[2].endswith("tuesday-b-collection-calendar/")
    assert {c.date for c in items} == {date(2026, 11, 10), date(2026, 11, 17)}


def test_home_not_found():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SEARCH)))
    with pytest.raises(NoCollectionsForAddress, match="no collection round"):
        chelmsford(LookupInput(postcode="CM3 7AE", house_number="99"), client=client)
