"""Somerset without a browser: the iTouchVision portal's encrypted endpoint (overrides.py).
The reply below is the one the portal gave for 7 Old Market Road, Bridgwater (Nov 2026)."""
import json
from datetime import date

import httpx
import pytest

from app.adapters.base import AdapterError, LookupInput, MissingInput, NoCollectionsForAddress
from app.adapters.overrides import ITOUCH_URL, itouch_crypt, somerset_direct
from app.adapters.registry import candidates

REAL_REPLY = {"collectionDay": [
    {"id": 48, "binType": "Rubbish Collection", "binColor": "#52595F", "description": "",
     "collectionDay": "18-11-2026"},
    {"id": 65, "binType": "Food Recycling", "binColor": "#0078BB", "description": "",
     "collectionDay": "11-11-2026"},
    {"id": 65, "binType": "Recycling Collection", "binColor": "#0078BB", "description": "",
     "collectionDay": "11-11-2026"}]}
# What the portal's own page sent for UPRN 10090857778, captured from the browser
CAPTURED_HEADER = ("4e2e59840ac55505fd06ba509f871b87d186cd31d9fde634a9290dfb4d11ab6bff7bc96d4349e6"
                   "2a545487a876bfd4c2e7c0fe3e2754d4ce68c339bdda9963fe5f071441e16620b4d95d926ec48793"
                   "d7d606b70cc557465ec2ea80a55aefbbb6")


def _client(seen, reply=REAL_REPLY, raw=None, status=200):
    def handler(req: httpx.Request):
        assert str(req.url) == ITOUCH_URL and req.method == "GET"
        seen.append(json.loads(itouch_crypt(bytes.fromhex(req.headers["P_PARAMETER"]), False)))
        text = raw if raw is not None else itouch_crypt(json.dumps(reply).encode(), True).hex().upper()
        return httpx.Response(status, text=text)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reads_each_bins_next_collection():
    seen = []
    items = somerset_direct(LookupInput(postcode="TA6 4AA", uprn="10090857778"), client=_client(seen))
    assert seen == [{"P_UPRN": "10090857778", "P_CLIENT_ID": 129, "P_COUNCIL_ID": 34493,
                     "P_LANG_CODE": "EN"}]
    assert [(i.label, i.date) for i in items] == [
        ("Rubbish Collection", date(2026, 11, 18)), ("Food Recycling", date(2026, 11, 11)),
        ("Recycling Collection", date(2026, 11, 11))]


def test_matches_what_the_portal_page_sends():
    assert json.loads(itouch_crypt(bytes.fromhex(CAPTURED_HEADER), False)) == {
        "P_UPRN": "10090857778", "P_CLIENT_ID": 129, "P_COUNCIL_ID": 34493, "P_LANG_CODE": "EN"}


def test_needs_a_uprn():
    with pytest.raises(MissingInput):
        somerset_direct(LookupInput(postcode="TA6 4AA", house_number="5"), client=_client([]))


def test_empty_list_is_no_collections():
    with pytest.raises(NoCollectionsForAddress):
        somerset_direct(LookupInput(postcode="TA6 4AA", uprn="1"),
                        client=_client([], reply={"collectionDay": []}))


def test_error_page_is_a_readable_error():
    with pytest.raises(AdapterError, match="HTTP 502, unreadable reply"):
        somerset_direct(LookupInput(postcode="TA6 4AA", uprn="1"),
                        client=_client([], raw="<html>Bad gateway</html>", status=502))


def test_goes_before_the_browser_scraper():
    mods = [s.module for s in candidates("E06000066", "Somerset")]
    assert mods[:2] == ["BinBotSomersetDirect", "SomersetCouncil"]
    assert candidates("E06000066", "Somerset")[0].requires == frozenset({"uprn"})
