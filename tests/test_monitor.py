"""Stage 2: diffing, change propagation, adapter breakage. Uses a fake adapter, no network."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.adapters.base import AdapterError, RawCollection
from app.models import ChangeLog, CollectionEvent, CouncilStatus, Outbox, Reminder, User
from app.services import schedule
from app.workers import monitor


@pytest.fixture
def fake_adapter(monkeypatch):
    """Replace the council fetch with a controllable list of collections."""
    state = {"items": [], "error": None}

    def fetch(council, prop):
        if state["error"]:
            raise state["error"]
        return state["items"]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    # Leave demo_mode on for postcode/address, but route fetching through the fake.
    return state


@pytest.fixture(autouse=True)
def pinned_week(monkeypatch):
    """Verify rotates which neighbours it asks by week; pin it so tests know who's asked."""
    from app.services import verify

    week = {"n": 0}
    monkeypatch.setattr(verify, "_week", lambda today: week["n"])
    return week


def _signup_active(client, db):
    uid = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123"})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return db.get(User, uid)


def test_change_detected_and_user_notified(client, db, fake_adapter):
    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)
    fake_adapter["items"] = [RawCollection("Recycling", tue + timedelta(weeks=i)) for i in range(4)]
    user = _signup_active(client, db)
    prop = user.property
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "dates_changed")) is None  # first fetch is not a "change"

    # Council moves one collection by a day (bank holiday).
    moved = tue + timedelta(weeks=2)
    fake_adapter["items"] = [
        RawCollection("Recycling", d + (timedelta(days=1) if d == moved else timedelta()))
        for d in (tue + timedelta(weeks=i) for i in range(4))
    ]
    res = schedule.refresh_round(db, prop)
    assert res.changed and res.diff["moved"] and not res.diff["added"] and not res.diff["removed"]
    ev = db.scalar(select(CollectionEvent).where(CollectionEvent.date == moved + timedelta(days=1)))
    assert ev.moved_from == moved

    # Monitor propagates: reminders rebuilt, user told.
    monitor._propagate_change(db, prop, res.diff)
    db.commit()
    msg = db.scalar(select(Outbox).where(Outbox.kind == "change"))
    assert msg and "moved a collection" in msg.body
    assert db.scalar(select(Reminder).where(Reminder.collection_date == moved + timedelta(days=1)))
    assert db.scalar(select(Reminder).where(Reminder.collection_date == moved)) is None
    log = db.scalar(select(ChangeLog).where(ChangeLog.kind == "dates_changed"))
    assert log and log.users_notified == 1


def test_unchanged_fetch_is_noop(client, db, fake_adapter):
    tue = date.today() + timedelta(days=7)
    fake_adapter["items"] = [RawCollection("Refuse", tue)]
    user = _signup_active(client, db)
    res = schedule.refresh_round(db, user.property)
    assert not res.changed
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "dates_changed")) is None


def test_adapter_breakage_flags_council_and_keeps_old_dates(client, db, fake_adapter):
    tue = date.today() + timedelta(days=7)
    fake_adapter["items"] = [RawCollection("Refuse", tue)]
    user = _signup_active(client, db)
    council = user.property.council
    fake_adapter["error"] = AdapterError("site changed", retryable=True)
    for _ in range(3):
        schedule.refresh_round(db, user.property)
    db.refresh(council)
    assert council.status == CouncilStatus.broken and council.failure_count == 3
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "adapter_broken"))
    # Old events survive a failed fetch, so reminders keep going.
    assert db.scalar(select(CollectionEvent).where(CollectionEvent.date == tue))

    fake_adapter["error"] = None
    monitor.health_check(db)
    db.refresh(council)
    assert council.status == CouncilStatus.ok
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "adapter_recovered"))


def test_refresh_stale_polls_harder_near_bank_holiday():
    assert monitor.near_bank_holiday(date(2026, 12, 10), 21)
    assert not monitor.near_bank_holiday(date(2026, 10, 1), 21)


def test_frequency_learned_from_weekly_refreshes(client, db, fake_adapter):
    """A council that only publishes 'your next collection' still yields a frequency once
    a few refreshes have been observed (past events are kept and used for the rule)."""
    from app.services.schedule import schedule_for
    from app.models import CollectionEvent

    first = date.today() + timedelta(days=3)
    fake_adapter["items"] = [RawCollection("Blue Bin", first)]
    user = _signup_active(client, db)
    assert schedule_for(db, user.property)[0]["frequency"] == "irregular"
    # Simulate two fortnightly refreshes having happened in the past.
    bt_id = db.scalar(select(CollectionEvent.bin_type_id))
    for back in (14, 28):
        db.add(CollectionEvent(round_key=user.property.round_key, bin_type_id=bt_id,
                               date=first - timedelta(days=back)))
    db.flush()
    assert schedule_for(db, user.property)[0]["frequency"] == "fortnightly"
    assert schedule_for(db, user.property)[0]["next"] == first.isoformat()


def test_failed_own_lookup_keeps_street_calendar_and_council_health(client, db, fake_adapter):
    """Regression (Glasgow, 31 Oct): 'These aren't my days' picked an address the council
    lists nothing for. That must not mark the whole council broken, and must not leave the
    user with an empty calendar."""
    from app.adapters.base import NoCollectionsForAddress
    from app.models import Council, Property

    sun = date.today() + timedelta(days=(6 - date.today().weekday()) % 7 or 7)
    fake_adapter["items"] = [RawCollection("Green Bin", sun + timedelta(weeks=i)) for i in range(4)]
    first = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    second = client.post("/api/signup/start", json={"postcode": "E8 3PB"}).json()
    assert second["shared"] and len(second["bins"]) == 1

    fake_adapter["error"] = NoCollectionsForAddress("council lists nothing for this UPRN")
    r = client.post(f"/api/users/{second['user_id']}/own-calendar", json={"uprn": "10000099"}).json()
    assert r["shared"] is True and r["error"] and len(r["bins"]) == 1 and r["bins"][0]["days"]
    db.expire_all()
    assert db.scalar(select(Council)).status == CouncilStatus.ok
    assert {p.round_key for p in db.scalars(select(Property))} == {"pc:E09000012:E8 3PB"}


def test_verify_fills_missing_recycling_from_a_neighbour(client, db, fake_adapter, monkeypatch):
    """Loader-time verify: this address lists no recycling, a neighbour on the same postcode
    does. Fill it, show it, and don't treat it as 'removed' on the next refresh."""
    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)
    refuse = [RawCollection("Refuse", tue + timedelta(weeks=2 * i)) for i in range(3)]
    recycling = [RawCollection("Recycling", tue + timedelta(weeks=2 * i + 1)) for i in range(3)]
    asked = []

    def fetch(council, prop):
        asked.append(prop.house_number)
        return refuse + (recycling if prop.house_number == "10" else [])

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    start = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    assert [b["label"] for b in start["bins"]] == ["Refuse"]

    bins = client.post(f"/api/users/{start['user_id']}/verify").json()
    assert sorted(b["label"] for b in bins) == ["Recycling", "Refuse"]
    assert "10" in asked
    rec = next(b for b in bins if b["label"] == "Recycling")
    assert rec["frequency"] == "fortnightly" and rec["days"] == ["TUE"]

    user = db.get(User, start["user_id"])
    res = schedule.refresh_round(db, user.property)  # address calendar still lacks recycling
    assert not res.diff.get("removed")
    assert len(client.post(f"/api/users/{start['user_id']}/verify").json()) == 2
    n = len(asked)
    client.post(f"/api/users/{start['user_id']}/verify")
    assert len(asked) == n  # verified this week already: no more council lookups


def test_failed_address_is_rescued_by_a_neighbour(client, db, fake_adapter, monkeypatch):
    """The council can't find this address; a neighbour on the postcode works. The resident
    gets the street's calendar instead of a blank form."""
    from app.adapters.base import NoCollectionsForAddress

    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)

    def fetch(council, prop):
        if prop.house_number in (None, "12"):
            raise NoCollectionsForAddress("nothing for this address")
        return [RawCollection("Refuse", tue + timedelta(weeks=i)) for i in range(4)]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    assert r["error"] is None and [b["label"] for b in r["bins"]] == ["Refuse"]
    assert r["bins"][0]["frequency"] == "weekly"


def test_uprn_in_url_template_is_detected_and_filled():
    """Regression (Warwick, Shropshire, Pembrokeshire...): the UPRN lives in the URL, so
    input.json has no 'uprn' key. We must ask for one and substitute any-length XXXX."""
    from app.adapters.registry import load_specs

    specs = {s.module: s for s in load_specs().values()}
    w = specs["WarwickDistrictCouncil"]
    assert w.needs_uprn and w.placeholder == "XXXXXXXX" and w.sample_id == "100070263793"
    sh = specs["ShropshireCouncil"]
    assert sh.needs_uprn and len(sh.placeholder) == 11
    k = specs.pop("KingstonUponThamesCouncil")  # WasteWorks now: resolves a UPRN itself
    assert k.url == "https://waste-services.kingston.gov.uk/" and not k.placeholder
    assert "KingstonUponThamesCouncil" not in specs  # council-specific id: not routable yet


def _verify_log(db):
    return [c.diff for c in db.scalars(select(ChangeLog).where(ChangeLog.kind == "verified"))]


def test_verify_confirms_the_round_with_one_neighbour(client, db, fake_adapter, monkeypatch):
    """Nothing missing, but the round still rests on one scrape: one matching neighbour
    confirms it, and that's the only extra lookup."""
    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)
    asked = []

    def fetch(council, prop):
        asked.append(prop.house_number)
        return [RawCollection("Refuse", tue + timedelta(weeks=i)) for i in range(4)] + \
               [RawCollection("Recycling", tue + timedelta(weeks=i)) for i in range(4)]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    start = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    client.post(f"/api/users/{start['user_id']}/verify")
    assert asked == [None, "10"]
    assert _verify_log(db)[0]["status"] == "confirmed"


def test_loader_verify_spends_no_lookup_when_nothing_is_missing(client, db, fake_adapter,
                                                                  monkeypatch):
    """The loader's call only asks the council when a bin is missing; confirming the round is
    left to the background call."""
    from app.services.verify import verify_round

    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)
    asked = []

    def fetch(council, prop):
        asked.append(prop.house_number)
        return [RawCollection("Refuse", tue + timedelta(weeks=i)) for i in range(4)] + \
               [RawCollection("Recycling", tue + timedelta(weeks=i)) for i in range(4)]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    start = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    user = db.get(User, start["user_id"])
    assert verify_round(db, user.property, confirm=False)["looked_at"] == 0
    assert asked == [None] and not _verify_log(db)


def test_outvoted_neighbour_is_logged_and_not_used(client, db, fake_adapter, monkeypatch):
    """First neighbour says Wednesday (another round, or a bad scrape), second says Tuesday
    like us: 2 of 3, confirmed. Its recycling isn't borrowed; the agreeing one's is."""
    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)

    def fetch(council, prop):
        day = tue + timedelta(days=1) if prop.house_number == "10" else tue
        refuse = [RawCollection("Refuse", day + timedelta(weeks=i)) for i in range(4)]
        recycling = [RawCollection("Recycling", day + timedelta(weeks=i, days=2)) for i in range(4)]
        return refuse + ([] if prop.house_number is None else recycling)

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    start = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    bins = client.post(f"/api/users/{start['user_id']}/verify").json()

    rec = next(b for b in bins if b["label"] == "Recycling")
    assert rec["next"] == (tue + timedelta(days=2)).isoformat()  # from 16, not from 10
    v = _verify_log(db)[0]
    assert v["status"] == "confirmed" and v["agreed"] == ["16"] and v["differed"][0]["from"] == "10"
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "neighbour_differs"))


def test_round_every_neighbour_contradicts_is_disputed(client, db, fake_adapter, monkeypatch):
    """Our scrape says Tuesday, both neighbours say Thursday: ops need to look. The resident
    still gets one answer (theirs, from the council for their address)."""
    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)

    def fetch(council, prop):
        day = tue if prop.house_number is None else tue + timedelta(days=2)
        return [RawCollection("Refuse", day + timedelta(weeks=i)) for i in range(4)] + \
               [RawCollection("Recycling", day + timedelta(weeks=i)) for i in range(4)]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    start = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    bins = client.post(f"/api/users/{start['user_id']}/verify").json()
    assert {b["days"][0] for b in bins} == {"TUE"}
    assert _verify_log(db)[0]["status"] == "disputed"
    d = db.scalar(select(ChangeLog).where(ChangeLog.kind == "disputed"))
    assert [x["from"] for x in d.diff["differed"]] == ["10", "16"]


def test_rescue_uses_first_neighbour_and_confirms_later(client, db, fake_adapter, monkeypatch):
    """This address fails: the resident gets the first neighbour's calendar after one extra
    lookup, not two. Confirming happens afterwards, with a different home than the one we
    borrowed from (asking that one again would only agree with itself)."""
    from app.adapters.base import NoCollectionsForAddress

    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)
    asked = []

    def fetch(council, prop):
        asked.append(prop.house_number)
        if prop.house_number is None:
            raise NoCollectionsForAddress("nothing for this address")
        day = tue if prop.house_number == "10" else tue + timedelta(days=3)
        return [RawCollection("Refuse", day + timedelta(weeks=i)) for i in range(4)]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    r = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    assert r["error"] is None and r["bins"][0]["days"] == ["TUE"]
    assert asked.count("10") == 1 and "16" not in asked
    rescued = db.scalar(select(ChangeLog).where(ChangeLog.kind == "rescued"))
    assert rescued.diff == {"from": "10"}

    client.post(f"/api/users/{r['user_id']}/verify")  # confirm runs after the response
    assert asked.count("10") == 1 and "16" in asked
    assert _verify_log(db)[0]["status"] == "disputed"
    n = len(asked)
    client.post(f"/api/users/{r['user_id']}/verify")
    assert len(asked) == n


def test_verify_asks_different_homes_each_week(client, db, fake_adapter, monkeypatch, pinned_week):
    """Over a few weeks every home on the postcode confirms the round, not the same two."""
    from app.services import verify

    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)
    fake_adapter["items"] = [RawCollection("Refuse", tue + timedelta(weeks=i)) for i in range(4)] + \
                            [RawCollection("Recycling", tue + timedelta(weeks=i)) for i in range(4)]
    start = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    prop = db.get(User, start["user_id"]).property
    asked = set()
    for week in range(4):
        pinned_week["n"] = week
        asked |= {n.house_number for n in verify._neighbours(db, prop)}
    assert asked == {"10", "14", "16", "Flat 3, 18"}


def test_residents_on_the_round_are_asked_first(client, db, fake_adapter, monkeypatch):
    """A neighbour who signed up is on the round: their calendar is the one worth confirming."""
    from app.services import verify

    tue = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)
    fake_adapter["items"] = [RawCollection("Refuse", tue + timedelta(weeks=i)) for i in range(4)]
    first = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()
    client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000004",
                                           "house_number": "16"})
    prop = db.get(User, first["user_id"]).property
    assert verify._neighbours(db, prop)[0].house_number == "16"
