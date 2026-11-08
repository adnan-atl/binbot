"""Cotswold without a browser: the council's Salesforce flow, asked directly (overrides.py).
The fake transport below answers the way community.cotswold.gov.uk did when this was built."""
import json
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.base import LookupInput, NoCollectionsForAddress
from app.adapters.overrides import _cotswold_pick, cotswold_direct
from app.adapters.registry import candidates

PAGE = '<script>var x = {"fwuid":"FW123","loaded":{"APPLICATION@markup://siteforce:communityApp":"1913_abc"}};</script>'
REC = {"id": "a0K1", "fields": {"Name": {"value": "26 LONG STREET, TETBURY, GL8 8AQ"}}}
ROWS = [{"col1": "Black Recycling Box", "col2": "Fri, 20 November"},
        {"col1": "Outdoor Food Caddy", "col2": "Fri, 13 November"}]


def _transport(calls, final_status="STARTED", records=(REC,)):
    def handler(req: httpx.Request):
        if req.method == "GET":
            return httpx.Response(200, text=PAGE)
        form = parse_qs(req.content.decode())
        ctx = json.loads(form["aura.context"][0])
        assert ctx["fwuid"] == "FW123" and form["aura.token"] == ["null"]
        action = json.loads(form["message"][0])["actions"][0]
        name = action["descriptor"].rsplit("$", 1)[-1]
        calls.append((name, action["params"].get("request", {}).get("action")))
        if name == "startFlow":
            args = json.loads(action["params"]["arguments"])
            assert args[0]["name"] == "vClientCode" and args[0]["value"] == "CDC"
            rv = {"response": {"interviewStatus": "STARTED", "serializedEncodedState": "S0"}}
        elif name == "lookup":
            rv = {"lookupResults": {"Property__c": {"records": list(records)}}}
        else:
            step = sum(1 for c in calls if c[0] == "navigateFlow")
            status = "NEEDS_COMMIT" if step < 3 else final_status
            rv = {"response": {"interviewStatus": status, "serializedEncodedState": f"S{step}",
                               "fields": [{"name": "dataJson", "value": json.dumps(ROWS)}]
                               if status == "STARTED" else []}}
        body = json.dumps({"actions": [{"state": "SUCCESS", "returnValue": rv}]})
        return httpx.Response(200, text="while(1);" + body)
    return httpx.MockTransport(handler)


def test_reads_the_collection_table_without_a_browser():
    calls = []
    client = httpx.Client(transport=_transport(calls))
    items = cotswold_direct(LookupInput(postcode="GL8 8AQ", house_number="26",
                                        address_line="26, Long Street, Tetbury, GL8 8AQ"),
                            client=client, today=date(2026, 11, 8))
    assert [(i.label, i.date) for i in items] == [("Black Recycling Box", date(2026, 11, 20)),
                                                   ("Outdoor Food Caddy", date(2026, 11, 13))]
    assert [c for c in calls if c[0] == "navigateFlow"] == [
        ("navigateFlow", "NEXT"), ("navigateFlow", "CONTINUE_AFTER_COMMIT"),
        ("navigateFlow", "CONTINUE_AFTER_COMMIT")]


def test_dates_after_new_year_roll_over():
    calls = []
    client = httpx.Client(transport=_transport(calls))
    items = cotswold_direct(LookupInput(postcode="GL8 8AQ", house_number="26"), client=client,
                            today=date(2026, 12, 30))
    assert items[0].date == date(2027, 11, 20)  # "Fri, 20 November" is already past: next year


def test_unknown_address_is_not_a_council_failure():
    client = httpx.Client(transport=_transport([], records=()))
    with pytest.raises(NoCollectionsForAddress):
        cotswold_direct(LookupInput(postcode="GL8 8AQ", house_number="99"), client=client)


def test_picks_the_right_house():
    recs = [{"id": "x", "fields": {"Name": {"value": n}}} for n in
            ["260 LONG STREET, TETBURY, GL8 8AQ", "FLAT 2, 26 LONG STREET, TETBURY, GL8 8AQ",
             "26 LONG STREET, TETBURY, GL8 8AQ"]]
    assert _cotswold_pick(recs, "26", None)["fields"]["Name"]["value"].startswith("26 LONG")
    assert _cotswold_pick(recs, None, "26, Long Street")["fields"]["Name"]["value"].startswith("26 LONG")
    assert _cotswold_pick(recs, "7", None) is None


def test_it_goes_first_and_the_browser_scraper_is_the_fallback():
    mods = [s.module for s in candidates("E07000079", "Cotswold")]
    assert mods[:2] == ["BinBotCotswoldDirect", "CotswoldDistrictCouncil"]


def _fake(name, delay=0.0, items=None, error=None, calls=None):
    import time

    from app.adapters.base import RawCollection

    class A:
        def __init__(self):
            self.name = name

    a = A()

    def run(adapter, full):
        calls.append(name)
        time.sleep(delay)
        if error:
            raise error
        return items or [RawCollection(name, date(2026, 11, 20))]
    return a, run


@pytest.fixture
def hedge(monkeypatch):
    from app.services import schedule
    monkeypatch.setattr(schedule, "HEDGE_AFTER_SECONDS", 0.2)
    state = {"calls": [], "runs": {}}
    monkeypatch.setattr(schedule, "_fetch_one", lambda adapter, full: state["runs"][adapter.name](adapter, full))
    return schedule, state


def _setup(state, *specs):
    out = []
    for name, kw in specs:
        a, run = _fake(name, calls=state["calls"], **kw)
        state["runs"][name] = run
        out.append(a)
    return out


def test_quick_answer_never_starts_the_browser(hedge):
    schedule, state = hedge
    fast, slow = _setup(state, ("direct", {}), ("browser", {}))
    items = schedule._hedged(fast, slow, LookupInput(postcode="GL8 8AQ", house_number="26"))
    assert items[0].label == "direct" and state["calls"] == ["direct"]


def test_slow_quick_one_starts_the_browser_and_the_first_answer_wins(hedge):
    schedule, state = hedge
    fast, slow = _setup(state, ("direct", {"delay": 0.6}), ("browser", {"delay": 0.05}))
    items = schedule._hedged(fast, slow, LookupInput(postcode="GL8 8AQ", house_number="26"))
    assert items[0].label == "browser"  # one result recorded: the first back
    assert state["calls"] == ["direct", "browser"]


def test_failed_quick_one_falls_back_to_the_browser(hedge):
    from app.adapters.base import AdapterError

    schedule, state = hedge
    fast, slow = _setup(state, ("direct", {"error": AdapterError("site changed")}), ("browser", {}))
    assert schedule._hedged(fast, slow, LookupInput(postcode="GL8 8AQ"))[0].label == "browser"


def test_address_the_council_doesnt_list_doesnt_start_the_browser(hedge):
    schedule, state = hedge
    fast, slow = _setup(state, ("direct", {"error": NoCollectionsForAddress("none")}), ("browser", {}))
    with pytest.raises(NoCollectionsForAddress):
        schedule._hedged(fast, slow, LookupInput(postcode="GL8 8AQ"))
    assert state["calls"] == ["direct"]
