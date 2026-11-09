"""East Herts: the AchieveForms collection lookup, by UPRN (overrides.py). The rows below are
the shape eastherts-self.achieveservice.com returned in Nov 2026."""
import json
from datetime import date

import httpx
import pytest

from app.adapters.base import AdapterError, LookupInput, NoCollectionsForAddress
from app.adapters.overrides import EAST_HERTS_LOOKUP, east_herts, east_herts_rows

ROWS = {"RecyclingServiceName": "\n   Mixed Recycling (Black)\n  ",
        "RecyclingNextDate": "Tuesday 17th November",
        "PaperServiceName": "\n  Cardboard and Paper (Blue)\n ", "PaperNextDate": "Tuesday 24th November",
        "GWServiceName": "\n Garden\n ", "GWNextDate": "Thursday 12th November",
        "FoodServiceName": "Food", "FoodNextDate": "",
        "RefuseServiceName": "Refuse (Purple)", "RefuseNextDate": "Tuesday 5th January",
        "RecyclingImageURL": "https://example/black-bin.svg"}
TODAY = date(2026, 11, 9)


def test_rows():
    items = east_herts_rows(ROWS, TODAY)
    assert [(c.label, c.date) for c in items] == [
        ("Garden", date(2026, 11, 12)), ("Mixed Recycling (Black)", date(2026, 11, 17)),
        ("Cardboard and Paper (Blue)", date(2026, 11, 24)), ("Refuse (Purple)", date(2027, 1, 5))]


def _client(seen, reply):
    def handler(req: httpx.Request):
        if "isauthenticated" in req.url.path:
            return httpx.Response(200, json={"auth-session": "SID1"})
        seen.append((req.url.params["id"], req.url.params["sid"], json.loads(req.content)))
        return httpx.Response(200, json=reply)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_calls_the_current_lookup_with_the_uprn():
    seen = []
    items = east_herts(LookupInput(postcode="SG14 1AA", uprn="10023088183"),
                       client=_client(seen, {"integration": {"transformed": {"rows_data": {"0": ROWS}}}}),
                       today=TODAY)
    assert seen == [(EAST_HERTS_LOOKUP, "SID1",
                     {"formValues": {"Collection Days": {"inputUPRN": {"value": "10023088183"}}}})]
    assert len(items) == 4


def test_retired_lookup_is_a_clear_error():
    with pytest.raises(AdapterError, match="User Package"):
        east_herts(LookupInput(postcode="SG14 1AA", uprn="1"),
                   client=_client([], {"error": "User Package  Not Found"}), today=TODAY)


def test_no_dates_is_no_collections():
    with pytest.raises(NoCollectionsForAddress):
        east_herts(LookupInput(postcode="SG14 1AA", uprn="1"),
                   client=_client([], {"integration": {"transformed": {"rows_data": {"0": {}}}}}),
                   today=TODAY)
