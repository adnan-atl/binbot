"""South Hams: same two calls as the library, one consistent browser, readable errors."""
import json

import httpx
import pytest

from app.adapters.base import AdapterError, LookupInput, unreachable
from app.adapters.overrides import south_hams

TILE = ('<div class="collectionDiv"><h3>Refuse Collection Service</h3><div class="detWrap">'
        'Collected fortnightly on Tuesday. Your next scheduled collection is tomorrow, '
        'Tuesday, 10 November 2026.</div></div>'
        '<div class="collectionDiv"><h3>Recycling Collection Service</h3><div class="detWrap">'
        'Your next scheduled collection is Tuesday, 17 November 2026.</div></div>')


def _client(post):
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, text='<input name="fcc_session_token" value="T0K3N">',
                                  headers={"Set-Cookie": "fcc_session_cookie=T0K3N; Path=/"})
        assert b"fcc_session_token=T0K3N" in req.content and b"uprn=123" in req.content
        return post(req)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reads_the_collections():
    c = _client(lambda r: httpx.Response(200, json={"error": {}, "binCollections": {"tile": [[TILE]]}}))
    items = south_hams(LookupInput(postcode="TQ9 5NE", uprn="123"), client=c)
    assert [(i.label, i.date.isoformat()) for i in items] == [
        ("Refuse Collection Service", "2026-11-10"), ("Recycling Collection Service", "2026-11-17")]


def test_non_json_reply_says_what_came_back():
    c = _client(lambda r: httpx.Response(200, text=""))
    with pytest.raises(AdapterError, match="HTTP 200, not JSON"):
        south_hams(LookupInput(postcode="TQ9 5NE", uprn="123"), client=c)


def test_blocked_counts_as_unreachable_so_the_proxy_can_try():
    c = _client(lambda r: httpx.Response(403, text="<html>Forbidden</html>"))
    with pytest.raises(AdapterError) as got:
        south_hams(LookupInput(postcode="TQ9 5NE", uprn="123"), client=c)
    assert unreachable(str(got.value))
