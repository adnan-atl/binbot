"""Rotating bins: each bin carries its next real dates, so each confirm-screen row can show
how often it goes out and its next date."""
from datetime import date, timedelta

from sqlalchemy import select

from app.adapters.base import RawCollection
from app.models import User
from tests.test_fallbacks import _signup


def test_upcoming_dates_for_weekly_and_fortnightly_bins(client, db, monkeypatch):
    today = date.today()
    wed = today + timedelta(days=(2 - today.weekday()) % 7 or 7)
    weeks = lambda start, step, n: [start + timedelta(weeks=step * i) for i in range(n)]
    items = ([RawCollection("Recycling", d) for d in weeks(wed, 1, 8)]
             + [RawCollection("Residual waste", d) for d in weeks(wed, 2, 4)]
             + [RawCollection("Garden waste", d) for d in weeks(wed + timedelta(weeks=1), 2, 4)])
    _signup(client, db, monkeypatch, items)
    uid = db.scalar(select(User.id))
    bins = {b["label"]: b for b in client.get(f"/api/users/{uid}/bins").json()}

    iso = lambda ds: [d.isoformat() for d in ds]
    assert bins["Recycling"]["upcoming"] == iso(weeks(wed, 1, 4))
    assert bins["General waste (black bin)"]["upcoming"] == iso(weeks(wed, 2, 4))
    assert bins["Garden waste"]["upcoming"] == iso(weeks(wed + timedelta(weeks=1), 2, 4))
    # Residual and garden take turns: never on the same Wednesday
    assert not set(bins["General waste (black bin)"]["upcoming"]) & set(bins["Garden waste"]["upcoming"])
    assert bins["General waste (black bin)"]["next"] == wed.isoformat()


def test_manual_bins_get_upcoming_dates_too(client, db, monkeypatch):
    today = date.today()
    _signup(client, db, monkeypatch, [RawCollection("Recycling", today + timedelta(days=3))])
    uid = db.scalar(select(User.id))
    mon = today + timedelta(days=(0 - today.weekday()) % 7 or 7)
    out = client.put(f"/api/users/{uid}/bins", json={"bins": [
        {"label": "Glass", "category": "glass", "days": ["MON"], "frequency": "fortnightly",
         "anchor": mon.isoformat()}]}).json()
    glass = next(b for b in out if b["label"] == "Glass")
    assert glass["upcoming"] == [(mon + timedelta(weeks=2 * i)).isoformat() for i in range(4)]


def test_confirm_screen_has_no_next_collections_card(client):
    page = client.get("/").text
    assert 'id="c-next"' not in page and "Your next two collections" not in page
    assert "Your bins take turns" not in page
