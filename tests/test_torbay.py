"""Torbay: its ServiceBuilder form, posted with the UPRN, no browser (overrides.py).
Pages follow torbay.gov.uk / selfservice-torbay.servicebuilder.co.uk as in Nov 2026."""
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.base import AdapterError, LookupInput, MissingInput, NoCollectionsForAddress
from app.adapters.overrides import torbay, torbay_rows

PAGE = '<a href="https://selfservice-torbay.servicebuilder.co.uk/renderform?t=62&amp;k=ABC" target="_blank">Find your bin collection dates</a>'
FORM = ('<form><input name="__RequestVerificationToken" value="TOK"><input name="FormGuid" value="G-1">'
        '<input name="ObjectTemplateID" value="62"><input name="CurrentSectionID" value="0"></form>')
RESULT = ("<h2>Upcoming collections for: 1 Kathys Cottages, Torquay, TQ1 1AG</h2><table>"
          "<tr><td>Wednesday 11 November 2026</td><td>Recycling Collection Service</td></tr>"
          "<tr><td>Wednesday 18 November 2026</td><td>Domestic Collection Service</td></tr>"
          "<tr><td>Wednesday 18 November 2026</td><td>Recycling Collection Service</td></tr></table>"
          "<a>Check another property</a>")


def test_rows():
    assert [(c.label, c.date) for c in torbay_rows(RESULT)] == [
        ("Recycling Collection Service", date(2026, 11, 11)),
        ("Domestic Collection Service", date(2026, 11, 18)),
        ("Recycling Collection Service", date(2026, 11, 18))]


def _client(posted, result=RESULT, page=PAGE):
    def handler(req: httpx.Request):
        if req.method == "POST":
            posted.append(parse_qs(req.content.decode()))
            return httpx.Response(200, text=result)
        if "torbay.gov.uk" in req.url.host:
            return httpx.Response(200, text=page)
        assert req.url.params["k"] == "ABC"
        return httpx.Response(200, text=FORM)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_posts_the_form_with_the_uprn():
    posted = []
    items = torbay(LookupInput(postcode="TQ1 1AG", uprn="10000016984"), client=_client(posted))
    assert posted[0]["FF1168"] == ["U10000016984"] and posted[0]["__RequestVerificationToken"] == ["TOK"]
    assert posted[0]["FormGuid"] == ["G-1"] and len(items) == 3


def test_needs_uprn():
    with pytest.raises(MissingInput):
        torbay(LookupInput(postcode="TQ1 1AG"), client=_client([]))


def test_no_rows():
    with pytest.raises(NoCollectionsForAddress):
        torbay(LookupInput(postcode="TQ1 1AG", uprn="1"), client=_client([], result="<p>Nothing</p>"))


def test_link_gone():
    with pytest.raises(AdapterError, match="form link not found"):
        torbay(LookupInput(postcode="TQ1 1AG", uprn="1"), client=_client([], page="<p>moved</p>"))
