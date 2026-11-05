"""A bin collected (nearly) every day is shown with a note but never reminded about: otherwise
there'd be a message every evening."""
from sqlalchemy import select

from app.models import Reminder
from app.services.normalize import is_daily
from tests.test_flow import signup

ALL = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _turn_on(client, uid):
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123", "consent": True})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})


def _reminded_labels(db, uid):
    db.expire_all()
    return {b["label"] for r in db.scalars(select(Reminder).where(Reminder.user_id == uid))
            for b in r.bins}


def test_is_daily():
    assert is_daily(ALL) and is_daily(ALL[:5]) and is_daily(ALL[:6])
    assert not is_daily(["TUE", "FRI"]) and not is_daily(ALL[:4])


def test_daily_bin_is_shown_but_never_reminded(client, db):
    uid = signup(client)["user_id"]
    bins = client.get(f"/api/users/{uid}/bins").json()
    body = {"bins": [{"setting_id": b["setting_id"], "label": b["label"], "category": b["category"]}
                     for b in bins]}
    body["bins"][1].update(days=ALL, frequency="weekly")  # the black bin goes out every day
    daily_label = body["bins"][1]["label"]
    out = client.put(f"/api/users/{uid}/bins", json=body).json()
    assert [b["daily"] for b in out] == [False, True, False, False]
    _turn_on(client, uid)
    labels = _reminded_labels(db, uid)
    assert labels and daily_label not in labels
    # Weekdays only counts as daily too
    body["bins"][1].update(days=ALL[:5])
    client.put(f"/api/users/{uid}/bins", json=body)
    assert daily_label not in _reminded_labels(db, uid)
    # Twice a week is still reminded
    body["bins"][1].update(days=["TUE", "FRI"])
    client.put(f"/api/users/{uid}/bins", json=body)
    assert daily_label in _reminded_labels(db, uid)


def test_whatsapp_summary_says_no_reminders_for_daily_bins():
    from app.api.schemas import BinOut
    from app.services.wa_signup import _bins_reply

    bins = [BinOut(label="Recycling Collection", category="recycling", days=["TUE", "FRI"],
                   frequency="weekly", next="2026-11-06"),
            BinOut(label="Rubbish Collection", category="general", days=ALL, frequency="weekly",
                   next="2026-11-06", daily=True)]
    text = _bins_reply("1 High Street", bins).text
    assert "*Recycling Collection*: Every week on Tuesday and Friday. Next: Fri 6 Nov" in text
    assert "*Rubbish Collection*: Collected every day, so no reminders for this one" in text
