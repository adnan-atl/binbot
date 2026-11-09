"""Ipswich: looked up by street (from the address), stretches narrowed by odd/even numbers.
Pages follow app.ipswich.gov.uk/bin-collection as in Nov 2026."""
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.base import AdapterError, LookupInput, NoCollectionsForAddress
from app.adapters.overrides import ipswich, ipswich_calendar


def entry(day, n, month, bins):
    lis = "".join(f'<li class="{c}">{t}</li>' for c, t in bins)
    return (f'<div><dt class="ibc-calendar-entry"><div class="ibc-calendar-entry__day">{day}</div>'
            f'<div class="ibc-calendar-entry__date">{n}<span class="ibc-visually-hidden">th</span></div>'
            f'<div class="ibc-calendar-entry__month">{month}</div></dt>'
            f'<dd class="ibc-calendar-entry__details"><p>To be collected:</p><ul>{lis}</ul></dd></div>')


CAL_A = ('<dl class="ibc-calendar-grid">'
         + entry("Thursday", 12, "November 2026", [("food", "Large food waste caddy"), ("black", "Black refuse bin")])
         + entry("Thursday", 19, "November 2026", [("blue", "Blue recycling bin")]) + "</dl>")
CAL_B = CAL_A.replace(">12<", ">13<").replace("Thursday", "Friday")
STRETCHES = ('<p>returned the following matches</p>'
             '<a href="/bin-collection/weeks/799">Norwich Road: St Matthews to Valley Road (even numbers only)</a>'
             '<a href="/bin-collection/weeks/800">Norwich Road: St Matthews to Valley Road (odd numbers only)</a>'
             '<a href="/bin-collection/weeks/801">Norwich Road: Valley Road to Railway Bridge</a>'
             '<a href="/bin-collection/weeks/900">Norwich Close</a>')


def test_calendar():
    assert [(c.label, c.date) for c in ipswich_calendar(CAL_A)] == [
        ("Large food waste caddy", date(2026, 11, 12)), ("Black refuse bin", date(2026, 11, 12)),
        ("Blue recycling bin", date(2026, 11, 19))]


def _client(seen, search, weeks):
    def handler(req: httpx.Request):
        if req.method == "POST":
            street = parse_qs(req.content.decode())["street-name"][0]
            seen.append(street)
            return httpx.Response(200, text=search.get(street, "<p>No results</p>"))
        seen.append(req.url.path)
        return httpx.Response(200, text=weeks[req.url.path.rsplit("/", 1)[-1]])
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_single_street_shows_the_calendar():
    seen = []
    items = ipswich(LookupInput(postcode="IP1 4DS", house_number="3",
                                address_line="3, SILOAM PLACE, IPSWICH, IP3 0BG"),
                    client=_client(seen, {"SILOAM PLACE": CAL_A}, {}))
    assert seen == ["SILOAM PLACE"] and len(items) == 3


def test_stretches_by_parity_when_they_agree():
    seen = []
    items = ipswich(LookupInput(postcode="IP1 4DS", house_number="12",
                                address_line="12, NORWICH ROAD, IPSWICH, IP1 4DS"),
                    client=_client(seen, {"NORWICH ROAD": STRETCHES},
                                   {"799": CAL_A, "800": CAL_B, "801": CAL_A}))
    assert "/bin-collection/weeks/800" not in seen  # odd side skipped for number 12
    assert "/bin-collection/weeks/900" not in seen  # Norwich Close is another street
    assert len(items) == 3


def test_stretches_that_differ_are_not_guessed():
    with pytest.raises(AdapterError, match="stretches with different days"):
        ipswich(LookupInput(postcode="IP1 4DS", house_number="12",
                            address_line="12, NORWICH ROAD, IPSWICH, IP1 4DS"),
                client=_client([], {"NORWICH ROAD": STRETCHES}, {"799": CAL_A, "801": CAL_B}))


def test_unknown_street():
    with pytest.raises(NoCollectionsForAddress):
        ipswich(LookupInput(postcode="IP1 4DS", address_line="1, NOWHERE, IP1 4DS"),
                client=_client([], {}, {}))
