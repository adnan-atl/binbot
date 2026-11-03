"""After 7am a collection today no longer shows as "next": it's as good as gone."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.api import routes
from app.models import User
from tests.test_flow import signup

LONDON = ZoneInfo("Europe/London")


def test_todays_collection_drops_off_after_the_cutoff(client, db):
    uid = signup(client)["user_id"]
    user = db.get(User, uid)
    before = routes._bins_out(db, user)
    first = min(date.fromisoformat(b.upcoming[0]) for b in before if b.upcoming)
    due_today = [b.label for b in before if b.upcoming and b.upcoming[0] == first.isoformat()]
    assert due_today

    early = routes._bins_out(db, user, now=datetime.combine(first, time(6, 30), LONDON))
    late = routes._bins_out(db, user, now=datetime.combine(first, time(9, 15), LONDON))
    for b in early:
        if b.label in due_today:
            assert b.upcoming[0] == first.isoformat() and b.next == first.isoformat()
    for b in late:
        if b.label in due_today:
            assert first.isoformat() not in b.upcoming
            assert b.upcoming and b.next == b.upcoming[0] > first.isoformat()


def test_first_showable_day():
    d = date(2026, 11, 3)
    assert routes._first_showable_day(datetime.combine(d, time(6, 59), LONDON)) == d
    assert routes._first_showable_day(datetime.combine(d, time(7, 0), LONDON)) == date(2026, 11, 4)
