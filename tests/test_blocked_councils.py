"""Councils that block automated lookups (registry.BLOCKED) are never asked: the resident
goes straight to typing their days in, with a link to the council's own bin-day page, and
the next home on the postcode gets those days."""
from app.adapters.registry import BLOCKED
from app.services import schedule

HACKNEY = "E09000012"  # the test postcode's council; blocked for these tests
PAGE = "https://example.gov.uk/bin-days"


def _start(client, uprn):
    r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": uprn})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_blocked_council_is_not_asked_and_the_resident_gets_its_checker_link(client, db, monkeypatch):
    def never(council, prop):
        raise AssertionError("asked a council that blocks us")

    monkeypatch.setitem(BLOCKED, HACKNEY, PAGE)
    monkeypatch.setattr(schedule, "fetch_collections", never)
    first = _start(client, "10000002")
    assert first["bins"] == [] and not first["council_down"]
    assert first["checker_url"] == PAGE

    r = client.put(f"/api/users/{first['user_id']}/bins", json={"removed": [], "bins": [
        {"label": "General waste", "category": "general", "days": ["TUE"], "frequency": "fortnightly",
         "anchor": "2026-11-10"}]})
    assert r.status_code == 200, r.text
    second = _start(client, "10000003")
    assert second["shared"] is True and len(second["bins"]) == 1 and second["checker_url"] is None


def test_other_councils_have_no_checker_link(client):
    r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    assert r["checker_url"] is None and r["bins"]


def test_the_councils_we_know_block_us_are_listed():
    assert {"E07000225", "E07000106", "E07000136", "S12000011"} <= set(BLOCKED)
    assert all(u.startswith("https://") for u in BLOCKED.values())
