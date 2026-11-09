"""The district checker: finds districts, checks one home in each, summarises by council."""
import json

import httpx
import pytest

from app.adapters.base import AdapterError, NoCollectionsForAddress, RawCollection
from app.services.geo import AddressHit
from app.tools import district_sweep as ds


@pytest.fixture
def tmp_out(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "OUT", tmp_path)
    return tmp_path


def test_finds_districts_and_every_council_in_them(tmp_out, monkeypatch):
    monkeypatch.setattr(ds, "candidate_outcodes", lambda: ["GL8", "GL99", "SN14"])
    seq = {"GL8": iter(["E07000079", "E07000079", "E06000054"]), "SN14": iter(["E06000054"] * 3)}

    def handler(req):
        p = req.url.path
        if p.startswith("/outcodes/"):
            return httpx.Response(200 if p.endswith(("GL8", "SN14")) else 404, json={"result": {}})
        oc = req.url.params["outcode"]
        gss = next(seq[oc])
        return httpx.Response(200, json={"result": {"postcode": f"{oc} 1AA", "admin_district": gss,
                                                    "codes": {"admin_district": gss}}})

    real = httpx.Client
    monkeypatch.setattr(ds.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), base_url="https://api.postcodes.io"))
    targets = ds.find_targets()
    assert sorted((t["outcode"], t["gss"]) for t in targets) == [
        ("GL8", "E06000054"), ("GL8", "E07000079"), ("SN14", "E06000054")]
    assert json.loads((tmp_out / "district_targets.json").read_text()) == targets


def _adapter(results):
    class A:
        def __init__(self, spec):
            self.spec = spec

        def fetch(self, lookup):
            r = results[self.spec.module]
            if isinstance(r, Exception):
                raise r
            return r
    return A


def test_check_tries_the_scrapers_in_order(monkeypatch):
    from app.adapters.registry import AdapterSpec
    specs = [AdapterSpec(module="Fast", gss_code="X", name="X", url=""),
             AdapterSpec(module="Browser", gss_code="X", name="X", url="")]
    monkeypatch.setattr(ds, "candidates", lambda g, n: specs)
    monkeypatch.setattr(ds, "_addresses", lambda pc:
                        [AddressHit(uprn="1", line="1 A Road", house_number="1")])
    from datetime import date
    monkeypatch.setattr(ds, "UKBCDAdapter", _adapter({"Fast": AdapterError("site changed"),
                                                      "Browser": [RawCollection("Refuse", date(2026, 11, 20))]}))
    row = ds.check({"outcode": "GL8", "gss": "X", "council": "X", "postcode": "GL8 1AA"})
    assert row["result"] == "ok (1 dates)" and row["scraper"] == "Browser"


def test_check_reports_what_went_wrong(monkeypatch):
    from app.adapters.registry import AdapterSpec
    monkeypatch.setattr(ds, "candidates", lambda g, n: [AdapterSpec(module="M", gss_code="X", name="X", url="")])
    monkeypatch.setattr(ds, "_addresses", lambda pc: [])
    assert ds.check({"outcode": "A1", "gss": "X", "council": "X", "postcode": "A1 1AA"})["result"] == "no address list"
    monkeypatch.setattr(ds, "_addresses", lambda pc:
                        [AddressHit(uprn=str(i), line=f"{i} Road", house_number=str(i)) for i in range(3)])
    monkeypatch.setattr(ds, "UKBCDAdapter", _adapter({"M": NoCollectionsForAddress("adapter returned no collections")}))
    assert ds.check({"outcode": "A1", "gss": "X", "council": "X", "postcode": "A1 1AA"})["result"] == "no collections listed"
    monkeypatch.setattr(ds, "UKBCDAdapter", _adapter({"M": AdapterError("HTTP 403 Forbidden")}))
    assert ds.check({"outcode": "A1", "gss": "X", "council": "X", "postcode": "A1 1AA"})["result"] == "blocked/down"


def test_summary_groups_failures_by_council():
    rows = [{"council": "South Hams", "outcode": "TQ9", "result": "failed", "error": "JSONDecodeError"},
            {"council": "South Hams", "outcode": "TQ7", "result": "ok (5 dates)", "error": ""},
            {"council": "Leeds", "outcode": "LS1", "result": "ok (4 dates)", "error": ""}]
    text = ds.summarise(rows)
    assert "3 districts × councils, 2 ok (67%)" in text
    assert "*South Hams*: 1/2 · TQ9 failed · `JSONDecodeError`" in text


def test_candidate_outcodes_cover_the_country():
    ocs = ds.candidate_outcodes()
    assert {"GL8", "SW1A", "EC1A", "W1D", "BT1", "ZE1", "M1"} <= set(ocs)


def test_address_lookups_wait_out_rate_limits(monkeypatch):
    monkeypatch.setattr(ds, "ADDRESS_GAP", 0)
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    calls = []

    def fetch(s, pc, paid_ok=True, errors=None, alert=True):
        calls.append((paid_ok, alert))
        if len(calls) < 3:
            errors.append("os_places: Client error '429 Too Many Requests'")
            return []
        return [AddressHit(uprn="1", line="1 A Road", house_number="1")]

    monkeypatch.setattr(ds.geo, "_fetch_addresses", fetch)
    assert ds._addresses("GL8 1AA")[0].uprn == "1"
    assert calls == [(False, False)] * 3  # free provider only, no Slack alert, retried


def test_api_keys_never_reach_the_logs():
    from app.services.geo import _redact
    msg = "Client error '429' for url 'https://api.os.uk/search/places/v1/postcode?postcode=GU47&key=SECRET123&dataset=DPA'"
    assert "SECRET123" not in _redact(msg) and "key=***" in _redact(msg)
