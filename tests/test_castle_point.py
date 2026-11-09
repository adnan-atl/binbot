"""Castle Point: street calendar, the street found from the resident's address (overrides.py).
Pages follow apps.castlepoint.gov.uk as it was in Nov 2026."""
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.base import LookupInput, NoCollectionsForAddress
from app.adapters.overrides import castle_point, castle_point_calendar, street_parts

SEARCH = ('<div class="contentContainer">'
          '<a href="index.cfm?fa=myStreet.displayDetails&roadID=4454">ALBERT ROAD - THUNDERSLEY</a>'
          '<a href="index.cfm?fa=myStreet.displayDetails&roadID=5001">ALBERT ROAD - SOUTH BENFLEET</a>'
          '<a href="index.cfm?fa=myStreet.displayDetails&roadID=5002">ALBERT CLOSE - THUNDERSLEY</a></div>')


def month(name, pink, normal):
    cells = "".join(f'<td class="pink DOY_1">{d}</td>' for d in pink)
    cells += "".join(f'<td class="normal DOY_2">{d}</td>' for d in normal)
    cells += '<td class="calendar textiles DOY_3">4</td>'
    return (f'<div class="calendarContainer"><table class="calendar"><tr class="calendar">'
            f'<td>{name}</td></tr><tr>{cells}</tr></table></div>')


CAL = ('<div class="contentContainer"><h1>About my Street - December 2026</h1>'
       '<div class="calendarContainer">' + month("December", [10, 24], [3, 17, 31])
       + month("January", [7], [14]) + '</div></div>')


def test_street_parts():
    assert street_parts("12, ALBERT ROAD, THUNDERSLEY, BENFLEET, SS7 1AB") == [
        "ALBERT ROAD", "THUNDERSLEY", "BENFLEET"]
    assert street_parts("Flat 2, 14a Kings Road, Benfleet SS7 1AB") == ["Kings Road", "Benfleet"]


def test_calendar_types_and_year_rollover():
    items = castle_point_calendar(CAL)
    by_day = {}
    for c in items:
        by_day.setdefault(c.date, set()).add(c.label)
    assert "Recycling (pink sacks)" in by_day[date(2026, 12, 10)]
    assert "Refuse (black sacks)" in by_day[date(2026, 12, 3)]
    assert "Refuse (black sacks)" not in by_day[date(2026, 12, 10)]
    assert {"Food waste", "Garden waste"} <= by_day[date(2026, 12, 3)]
    assert date(2027, 1, 7) in by_day and date(2027, 1, 14) in by_day  # January is next year
    assert date(2026, 12, 4) not in by_day  # textiles days are not collections


def test_no_calendar_means_no_collections():
    with pytest.raises(NoCollectionsForAddress):
        castle_point_calendar('<div class="contentContainer"><h1>About My Street</h1></div>')


def test_finds_the_road_id_from_the_address():
    posts = []

    def handler(req: httpx.Request):
        form = parse_qs(req.content.decode())
        posts.append((req.url.params.get("fa"), form))
        if req.url.params.get("fa") == "mystreet.search":
            return httpx.Response(200, text=SEARCH if form["searchterm"] == ["ALBERT ROAD"] else "")
        return httpx.Response(200, text=CAL)

    items = castle_point(LookupInput(postcode="SS7 1AB", uprn="100090000001",
                                     address_line="12, ALBERT ROAD, THUNDERSLEY, BENFLEET, SS7 1AB"),
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert posts[0] == ("mystreet.search", {"searchterm": ["ALBERT ROAD"]})
    assert posts[-1] == ("myStreet.displayDetails", {"roadID": ["4454"]})  # Thundersley, not Benfleet
    assert items


def test_unknown_street():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="")))
    with pytest.raises(NoCollectionsForAddress, match="street not found"):
        castle_point(LookupInput(postcode="SS7 1AB", address_line="1, NOWHERE LANE, SS7 1AB"),
                     client=client)
