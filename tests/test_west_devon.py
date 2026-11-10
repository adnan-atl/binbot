"""West Devon: the same FCC Environment site as South Hams, no browser (overrides.py).
The tile below is the shape westdevon.fccenvironment.co.uk returned in Nov 2026."""
import httpx
import pytest

from app.adapters.base import LookupInput, MissingInput, NoCollectionsForAddress
from app.adapters.overrides import WEST_DEVON, west_devon

TILE = ('<div class="collectionDiv"><h3>Recycling and Food Collection Service</h3><div class="detWrap">\n'
        '  Your recycling and food collection is  Weekly  on a  Friday .\n'
        '  Your next scheduled collection is  Friday, 13 November 2026 .</div></div>')


def _client(seen, reply):
    def handler(req: httpx.Request):
        seen.append((req.method, str(req.url), req.content.decode()))
        if req.method == "GET":
            return httpx.Response(200, text="<html></html>",
                                  headers={"Set-Cookie": "fcc_session_cookie=TOK; Path=/"})
        return httpx.Response(200, json=reply)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reads_the_next_collection_without_a_browser():
    seen = []
    items = west_devon(LookupInput(postcode="PL20 6DT", uprn="10013755313"),
                       client=_client(seen, {"error": {}, "binCollections": {"tile": [[TILE]]}}))
    assert [(i.label, i.date.isoformat()) for i in items] == [
        ("Recycling and Food Collection Service", "2026-11-13")]
    assert seen[0][:2] == ("GET", WEST_DEVON)
    assert seen[1][1] == WEST_DEVON + "/ajaxprocessor/getcollectiondetails"
    assert "fcc_session_token=TOK" in seen[1][2] and "uprn=10013755313" in seen[1][2]


def test_needs_a_uprn_and_reports_no_collections():
    with pytest.raises(MissingInput):
        west_devon(LookupInput(postcode="PL20 6DT", house_number="2"), client=_client([], {}))
    with pytest.raises(NoCollectionsForAddress):
        west_devon(LookupInput(postcode="PL20 6DT", uprn="1"),
                   client=_client([], {"binCollections": {"tile": []}}))
