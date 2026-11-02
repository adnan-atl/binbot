"""Splitting a postcode round that's really two (odd/even sides, two ends). No network."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.adapters.base import RawCollection
from app.models import ChangeLog, Outbox, Property, RoundSplit, User
from app.services import geo, schedule, sides, split
from app.services.geo import AddressHit
from app.workers import monitor

TUE = date.today() + timedelta(days=(1 - date.today().weekday()) % 7 + 7)
THU = TUE + timedelta(days=2)
KEY = "pc:E09000012:E8 3PB"


def number(uprn):
    return int(uprn[-2:])


@pytest.fixture(autouse=True)
def splits_live(monkeypatch):
    """These tests are about real splits; dry run has its own test."""
    from app.core.config import get_settings

    monkeypatch.setenv("SPLIT_DRY_RUN", "false")
    get_settings.cache_clear()


@pytest.fixture
def street(monkeypatch):
    """Homes 1-12 on one postcode; `day(n)` says which day the council gives house n."""
    state = {"day": lambda n: THU}
    hits = [AddressHit(uprn=f"200000{n:02d}", line=f"{n}, Long Road, London",
                       house_number=str(n)) for n in range(1, 13)]
    monkeypatch.setattr(geo, "lookup_addresses", lambda pc: hits)

    def fetch(council, prop):
        n = number(prop.uprn) if prop.uprn else sides.number_of(prop.house_number)
        d = state["day"](n)
        return [RawCollection("Refuse", d + timedelta(weeks=i)) for i in range(4)] + \
               [RawCollection("Recycling", d + timedelta(weeks=i)) for i in range(4)]

    monkeypatch.setattr(schedule, "fetch_collections", fetch)
    return state


def signup(client, n, phone=None):
    uid = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": f"200000{n:02d}"}).json()["user_id"]
    if phone:
        client.post(f"/api/users/{uid}/phone", json={"phone": phone})
        client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


def dispute(db, key=KEY):
    db.add(ChangeLog(scope="round", scope_id=key, kind="disputed", diff={}))
    db.commit()


def days_of(client, uid):
    return {b["days"][0] for b in client.get(f"/api/users/{uid}/bins").json()}


def test_house_numbers():
    assert sides.number_of("12") == 12 and sides.number_of("12A") == 12
    assert sides.number_of("Flat 3, 18") == 18 and sides.number_of("Rose Cottage") is None


def test_rules():
    assert sides.infer_rule([2, 4, 12], [1, 11])["by"] == "parity"
    cut = sides.infer_rule([1, 2, 3], [8, 9])
    assert cut["by"] == "threshold" and cut["at"] == 5.5 and cut["base"] == "lo"
    assert sides.infer_rule([2, 11, 12], [1, 4]) is None


def test_probe_tests_both_sides_and_both_ends():
    assert split._probe_picks(list(range(1, 13))) == [1, 2, 11, 12]


def test_odd_side_gets_its_own_round(client, db, street):
    """Evens are Thursday, odds Tuesday. The round's calendar came from 2 (Thursday), so 3
    was shown the wrong days. The probe finds two calendars split by parity, moves 3 to the
    odd side, fetches its days and tells them. New sign-ups land on the right side."""
    street["day"] = lambda n: TUE if n % 2 else THU
    a = signup(client, 2)
    b = signup(client, 3, phone="07700900123")
    assert days_of(client, b) == {"THU"}  # shared the postcode round: wrong side

    dispute(db)
    assert monitor.resolve_disputes(db) == {"split": 1}
    db.expire_all()
    rule = db.scalar(select(RoundSplit)).rule
    assert rule["by"] == "parity" and rule["sides"] == {"even": KEY, "odd": KEY + "~odd"}
    assert db.get(User, b).property.round_key == KEY + "~odd"
    assert days_of(client, b) == {"TUE"} and days_of(client, a) == {"THU"}
    assert db.scalar(select(Outbox).where(Outbox.kind == "change"))

    c = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "20000005"}).json()
    assert c["shared"] and days_of(client, c["user_id"]) == {"TUE"}
    assert split.disputed_rounds(db) == []


def test_failed_side_calendar_undoes_the_split(client, db, street, monkeypatch):
    """The other side's calendar won't load: residents go back AND the split rule goes, so
    new sign-ups don't land on a round with no calendar and the postcode can be probed again."""
    from app.services.schedule import RefreshResult

    street["day"] = lambda n: TUE if n % 2 else THU
    signup(client, 2)
    b = signup(client, 3)
    dispute(db)
    monkeypatch.setattr(split.sched, "refresh_round",
                        lambda db, prop, **kw: RefreshResult(snapshot=None, changed=False, diff={}, error="council down"))
    monitor.resolve_disputes(db)
    db.expire_all()
    assert db.scalar(select(RoundSplit)) is None
    assert db.get(User, b).property.round_key == KEY


def test_one_broken_round_does_not_stop_the_others(client, db, street, monkeypatch):
    """A probe that blows up is logged and marked checked; the next round still gets probed."""
    signup(client, 2)
    other = "pc:E09000012:E8 3PA"
    dispute(db, other)
    dispute(db)
    real = split.probe

    def probe(db, key):
        if key == other:
            raise KeyError("layout changed")
        return real(db, key)

    monkeypatch.setattr(split, "probe", probe)
    out = monitor.resolve_disputes(db)
    assert out["error"] == 1 and sum(out.values()) == 2
    assert other not in split.disputed_rounds(db)


def test_dry_run_only_logs_the_split(client, db, street, monkeypatch):
    """Default: say what would happen, change nothing, message nobody."""
    from app.core.config import get_settings

    monkeypatch.setenv("SPLIT_DRY_RUN", "true")
    get_settings.cache_clear()
    street["day"] = lambda n: TUE if n % 2 else THU
    signup(client, 2)
    b = signup(client, 3, phone="07700900123")
    dispute(db)
    assert monitor.resolve_disputes(db) == {"would_split": 1}
    checked = db.scalar(select(ChangeLog).where(ChangeLog.kind == "split_checked"))
    assert checked.diff["rule"]["by"] == "parity" and checked.diff["would_move"] == ["20000003"]
    assert db.scalar(select(RoundSplit)) is None
    assert db.get(User, b).property.round_key == KEY
    assert db.scalar(select(Outbox).where(Outbox.kind == "change")) is None
    assert split.disputed_rounds(db) == []  # probed: not again until a new vote


def test_split_side_verifies_only_against_its_own_side(client, db, street, monkeypatch):
    """After the split, the even side's verify mustn't ask odd homes and dispute itself."""
    from app.services import verify

    monkeypatch.setattr(verify, "_week", lambda today: 0)
    street["day"] = lambda n: TUE if n % 2 else THU
    a = signup(client, 2)
    signup(client, 3)
    dispute(db)
    monitor.resolve_disputes(db)
    db.expire_all()
    prop = db.get(User, a).property
    assert all(int(h.house_number) % 2 == 0 for h in verify._neighbours(db, prop))


def test_passing_bad_scrape_is_not_a_split(client, db, street):
    signup(client, 2)
    dispute(db)
    assert monitor.resolve_disputes(db) == {"one_calendar": 1}
    assert db.scalar(select(RoundSplit)) is None


def test_round_whose_own_calendar_is_the_odd_one_out(client, db, street):
    """Only house 2 says Thursday: that's a suspect scrape, not a second round."""
    street["day"] = lambda n: THU if n == 2 else TUE
    signup(client, 2)
    dispute(db)
    assert monitor.resolve_disputes(db) == {"anchor_outvoted": 1}
    assert db.scalar(select(RoundSplit)) is None


def test_two_calendars_with_no_pattern_are_left_for_ops(client, db, street):
    street["day"] = lambda n: TUE if n in (2, 11, 12) else THU
    signup(client, 2)
    dispute(db)
    assert monitor.resolve_disputes(db) == {"no_rule": 1}
    checked = db.scalar(select(ChangeLog).where(ChangeLog.kind == "split_checked"))
    assert checked.diff["groups"] == [[2, 11, 12], [1, 4]]
    assert db.scalar(select(RoundSplit)) is None
    assert {p.round_key for p in db.scalars(select(Property))} == {KEY}


# ---------------------------------------------------------------- residents' votes


def test_not_my_days_that_the_council_backs_up_triggers_a_split(client, db, street):
    """3 says "not my days"; the council gives 3 Tuesday where the round says Thursday.
    That's a vote against the round, and the probe finds the odd side."""
    street["day"] = lambda n: TUE if n % 2 else THU
    signup(client, 2)
    b = signup(client, 3)
    r = client.post(f"/api/users/{b}/own-calendar", json={}).json()
    assert r["error"] is None and days_of(client, b) == {"TUE"}
    vote = db.scalar(select(ChangeLog).where(ChangeLog.kind == "resident_differs"))
    assert vote.scope_id == KEY
    assert split.disputed_rounds(db) == [KEY]
    assert monitor.resolve_disputes(db) == {"split": 1}


def test_not_my_days_the_council_disagrees_with_is_for_ops(client, db, street):
    """Same dates from the council for their home: probing the street won't help, the
    council's own data is what's in question."""
    signup(client, 2)
    b = signup(client, 3)
    client.post(f"/api/users/{b}/own-calendar", json={})
    assert db.scalar(select(ChangeLog).where(ChangeLog.kind == "resident_reported"))
    assert split.disputed_rounds(db) == []


def test_wrong_reply_is_a_vote_against_the_round(client, db, street):
    from app.models import Reminder
    from app.services.reminders import dispatch_due

    uid = signup(client, 2, phone="07700900123")
    first = db.scalar(select(Reminder).order_by(Reminder.send_at))
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    assert "WRONG" in db.scalar(select(Outbox).where(Outbox.kind == "reminder")).body

    r = client.post("/api/inbound", json={"phone": "+447700900123", "text": "wrong"}).json()
    assert "noted" in r["reply"] and "double-check" not in r["reply"]
    vote = db.scalar(select(ChangeLog).where(ChangeLog.kind == "wrong_day"))
    assert vote.scope_id == KEY and vote.diff["user_id"] == uid
    assert split.disputed_rounds(db) == [KEY]


def test_a_homes_own_round_is_never_probed(db):
    db.add(ChangeLog(scope="round", scope_id="p:abc", kind="disputed", diff={}))
    db.commit()
    assert split.disputed_rounds(db) == []
