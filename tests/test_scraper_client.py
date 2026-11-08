"""The api waits out a scraper container restart (deploys) instead of failing the sign-up."""
import httpx
import pytest

from app.adapters.base import AdapterError, LookupInput
from app.adapters.registry import AdapterSpec
from app.scraper import client
from app.scraper.wire import result_to_json

SPEC = AdapterSpec(module="WiltshireCouncil", gss_code="E06000054", name="Wiltshire", url="")
LOOKUP = LookupInput(postcode="SP5 1AA", uprn="1")


def test_rides_out_a_restart(monkeypatch):
    monkeypatch.setattr(client, "RECONNECT_EVERY", 0)
    calls = []

    def post(url, **kw):
        calls.append(url)
        if len(calls) < 3:
            raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
        return httpx.Response(200, json=result_to_json([]), request=httpx.Request("POST", url))

    monkeypatch.setattr(client.httpx, "post", post)
    assert client.remote_fetch(SPEC, LOOKUP) == []
    assert len(calls) == 3


def test_gives_up_eventually(monkeypatch):
    monkeypatch.setattr(client, "RECONNECT_EVERY", 0)
    monkeypatch.setattr(client, "RECONNECT_FOR_SECONDS", 0)

    def post(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(client.httpx, "post", post)
    with pytest.raises(AdapterError, match="scraper service unavailable"):
        client.remote_fetch(SPEC, LOOKUP)
