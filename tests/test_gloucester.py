"""Gloucester without a browser: the council's AchieveForms lookups, asked directly."""
import json
from datetime import date

import httpx

from app.adapters.base import LookupInput
from app.adapters.overrides import gloucester
from app.adapters.registry import candidates

ANSWERS = {
    "63f72ddc8ca25": {"RefuseId": "R1", "RecyclingId": "C1", "FoodId": "", "GardenId": ""},
    "63f731d2b50d7": {"Refuse_Schedule1": "Monday Week 1", "Refuse1": "wfR"},
    "63f89f73018c0": {"Recycling_Schedule1": "Monday Weekly", "Recycling1": "wfC"},
    "63ca72c70c3b1": {"NextRefuse1DateISO": "2026-11-23"},
    "63cfcf4756b5d": {"NextRecycling1DateISO": "2026-11-16"},
}


def handler(req: httpx.Request):
    if "isauthenticated" in req.url.path:
        return httpx.Response(200, json={"auth-session": "SID"})
    lid = req.url.params["id"]
    assert req.url.params["sid"] == "SID"
    vals = json.loads(req.content)["formValues"]["Your waste collections"]
    assert vals["binUprn"]["value"] == "100120479510"
    return httpx.Response(200, json={"integration": {"transformed": {"rows_data": {"0": ANSWERS[lid]}}}})


def test_reads_each_bins_round_and_next_date():
    items = gloucester(LookupInput(postcode="GL2 0RR", uprn="100120479510"),
                       client=httpx.Client(transport=httpx.MockTransport(handler)), today=date(2026, 11, 10))
    by = {}
    for i in items:
        by.setdefault(i.label, []).append(i.date)
    assert by["General waste"][:2] == [date(2026, 11, 23), date(2026, 12, 7)]  # fortnightly
    assert by["Recycling"][:2] == [date(2026, 11, 16), date(2026, 11, 23)]  # weekly
    assert "Food waste" not in by


def test_goes_first_for_gloucester():
    assert candidates("E07000081", "Gloucester")[0].module == "BinBotGloucesterDirect"
