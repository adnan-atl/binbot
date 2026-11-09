"""Westminster: collection days per street (USRN), found from the resident's address.
The pages below follow transact.westminster.gov.uk as it was in Nov 2026."""
from datetime import date

import httpx
import pytest

from app.adapters.base import AdapterError, LookupInput, NoCollectionsForAddress
from app.adapters.overrides import westminster, westminster_days, westminster_services

SEARCH = ('<select id="dlstreets"><option value="">Select</option>'
          '<option value="8400202">A302</option><option value="8401095">Acacia Road</option>'
          '<option value="8401200">Acacia Place</option></select>')
REPORT = """<h2>Rubbish collections for</h2><table>
<tr><th>Location</th><th>Week Days</th><th>Week Times</th><th>Weekend Days</th><th>Weekend Times</th></tr>
<tr><td>Acacia Road</td><td>Tue, Fri</td><td>07:00 - 12:00</td><td></td><td></td></tr></table>
<h2>Recycling collections for</h2><table>
<tr><th>Location</th><th>Service Description</th><th></th><th>Week Days</th><th>Week Times</th><th>Weekend Days</th><th>Weekend Times</th></tr>
<tr><td>Acacia Road</td><td>Food Recycling Collection</td><td></td><td>Fri</td><td>08:00 - 14:00</td><td></td><td></td></tr>
<tr><td>Acacia Road</td><td>Recycling Collection</td><td></td><td>Fri</td><td>08:00 - 14:00</td><td></td><td></td></tr>
<tr><td>Acacia Road</td><td>Business Recycling (blue bags)</td><td></td><td>Mon - Fri</td><td></td><td></td><td></td></tr></table>
<h2>Street Cleaning Schedule for</h2><table>
<tr><th>Location</th><th>Service Description</th><th>Week Days</th><th>Weekend Days</th></tr>
<tr><td>Acacia Road</td><td>Your street will be swept on</td><td>Mon - Fri</td><td>Sat, Sun</td></tr></table>"""
MON = date(2026, 11, 9)


def _client(seen, report=REPORT, search=SEARCH):
    def handler(req: httpx.Request):
        seen.append(str(req.url))
        if req.url.path.endswith("streetsearch.aspx"):
            return httpx.Response(200, text=search)
        return httpx.Response(200, text=report)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_days():
    assert westminster_days("Mon - Fri") == {0, 1, 2, 3, 4}
    assert westminster_days("Sat, Sun") == {5, 6}
    assert westminster_days("Tue, Fri") == {1, 4}
    assert westminster_days("Fri - Mon") == {4, 5, 6, 0}
    assert westminster_days("") == set()


def test_services_skip_business_bags_and_street_cleaning():
    assert westminster_services(REPORT) == {"Rubbish Collection": {1, 4},
                                            "Food Recycling Collection": {4},
                                            "Recycling Collection": {4}}


def test_finds_the_street_usrn_from_the_address_not_the_uprn():
    seen = []
    items = westminster(LookupInput(postcode="NW8 6AB", uprn="100023456789",
                                    address_line="12, Acacia Road, London, NW8 6AB"),
                        client=_client(seen), today=MON)
    assert seen[-1].endswith("streetreport.aspx?USRN=8401095")  # the street, not the home's UPRN
    first = [(c.label, c.date) for c in items[:4]]
    assert first == [("Rubbish Collection", date(2026, 11, 10)),
                     ("Food Recycling Collection", date(2026, 11, 13)),
                     ("Recycling Collection", date(2026, 11, 13)),
                     ("Rubbish Collection", date(2026, 11, 13))]
    assert all(c.date > MON for c in items) and len({c.date for c in items}) == 16  # 8 weeks of Tue + Fri


def test_street_with_no_residential_collections():
    empty = REPORT.split("<h2>Recycling")[0].replace("Tue, Fri", "")
    with pytest.raises(NoCollectionsForAddress):
        westminster(LookupInput(postcode="NW8 6AB", address_line="1 Acacia Road, London"),
                    client=_client([], report=empty), today=MON)


def test_changed_page_is_a_clear_error():
    with pytest.raises(AdapterError, match="street list not found"):
        westminster(LookupInput(postcode="NW8 6AB", address_line="1 Acacia Road"),
                    client=_client([], search="<html>new site</html>"), today=MON)
