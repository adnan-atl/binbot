"""Torfaen: the same iTouchVision portal as Somerset, by UPRN, no browser (overrides.py).
The reply is the one the portal gave for 2 Rowan Crescent, Pontypool (Nov 2026)."""
import json
from datetime import date

import httpx

from app.adapters.base import LookupInput
from app.adapters.overrides import itouch_crypt, torfaen
from app.services.normalize import categorise

REPLY = {"collectionDay": [
    {"binType": "GREEN", "binColor": "#135700", "collectionDay": "10-11-2026",
     "followingDay": "24-11-2026", "description": None},
    {"binType": "PURPLE LIDDED BIN", "binColor": "#B307B3", "collectionDay": "17-11-2026",
     "followingDay": "01-12-2026", "description": None},
    {"binType": "RECYCLING & FOOD", "binColor": "#054FB9", "collectionDay": "10-11-2026",
     "followingDay": "17-11-2026", "description": None}]}


def test_both_dates_per_bin_with_readable_names():
    seen = []

    def handler(req: httpx.Request):
        seen.append(json.loads(itouch_crypt(bytes.fromhex(req.headers["P_PARAMETER"]), False)))
        return httpx.Response(200, text=itouch_crypt(json.dumps(REPLY).encode(), True).hex().upper())

    items = torfaen(LookupInput(postcode="NP4 5AA", uprn="100100804857"),
                    client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen == [{"P_UPRN": "100100804857", "P_CLIENT_ID": 80, "P_COUNCIL_ID": 397, "P_LANG_CODE": "EN"}]
    assert [(i.label, i.date) for i in items] == [
        ("Garden waste (green bin)", date(2026, 11, 10)), ("Garden waste (green bin)", date(2026, 11, 24)),
        ("General waste (purple lidded bin)", date(2026, 11, 17)),
        ("General waste (purple lidded bin)", date(2026, 12, 1)),
        ("Recycling and food waste", date(2026, 11, 10)), ("Recycling and food waste", date(2026, 11, 17))]
    assert categorise("Garden waste (green bin)").value == "garden"
    assert categorise("General waste (purple lidded bin)").value == "general"
