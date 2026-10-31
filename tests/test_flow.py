"""The six screens, end to end, in demo mode (no network)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import Outbox, Reminder, ReminderStatus
from app.services.reminders import dispatch_due


def signup(client, ref=None):
    r = client.post("/api/signup/start", json={"postcode": "e83pb", "uprn": "10000002",
                                               "house_number": "12", "referral_code": ref})
    assert r.status_code == 200, r.text
    return r.json()


def test_postcode_lookup(client):
    r = client.get("/api/postcode/E8%203PB")
    assert r.status_code == 200
    j = r.json()
    assert j["council"] == "Hackney" and j["supported"] is True
    assert j["addresses"] == [] and j["round_known"] is False  # no paid lookup by default
    j = client.get("/api/postcode/E8%203PB?addresses=true").json()
    assert len(j["addresses"]) == 5 and j["addresses"][0]["uprn"]
    assert client.get("/api/postcode/nope").status_code == 400


def test_full_flow(client, db):
    j = signup(client)
    uid = j["user_id"]
    cats = sorted(b["category"] for b in j["bins"])
    assert cats == ["food", "garden", "general", "recycling"]
    rec = next(b for b in j["bins"] if b["category"] == "recycling")
    assert rec["days"] == ["TUE"] and rec["frequency"] == "fortnightly" and rec["next"]

    # step 3: disable garden, move recycling to Tue+Fri weekly, add a glass bin
    garden = next(b for b in j["bins"] if b["category"] == "garden")
    r = client.put(f"/api/users/{uid}/bins", json={"bins": [
        {"setting_id": garden["setting_id"], "label": garden["label"], "category": "garden",
         "enabled": False},
        {"setting_id": rec["setting_id"], "label": "Recycling", "category": "recycling",
         "days": ["TUE", "FRI"], "frequency": "weekly"},
        {"label": "Glass", "category": "glass", "days": ["MON"], "frequency": "four_weekly"},
    ]})
    assert r.status_code == 200
    bins = {b["label"]: b for b in r.json()}
    assert bins["Recycling"]["days"] == ["TUE", "FRI"] and bins["Recycling"]["manual"]
    assert bins["Glass"]["frequency"] == "four_weekly"
    assert bins[garden["label"]]["enabled"] is False

    # step 4: phone (several formats accepted, stored as E.164)
    assert client.post(f"/api/users/{uid}/phone", json={"phone": "07700 900123"}).json()["phone"] == "+447700900123"
    assert client.post(f"/api/users/{uid}/phone", json={"phone": "+44 7700 900123"}).status_code == 200
    assert client.post(f"/api/users/{uid}/phone", json={"phone": "12345"}).status_code == 400

    # step 5: turn on reminders -> schedule built
    r = client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["scheduled"] > 0
    sched = client.get(f"/api/users/{uid}/schedule").json()
    assert all(s["send_at"].endswith("+00:00") or "T19:00" in s["send_at"] for s in sched)
    assert not any(b["category"] == "garden" for s in sched for b in s["bins"])
    assert any(b["category"] == "glass" for s in sched for b in s["bins"])

    # step 6: referral
    ref = client.get(f"/api/users/{uid}/referral").json()
    assert ref["level_name"] == "Bin rookie" and ref["referrals"] == 0 and ref["next_at"] == 1
    friend = signup(client, ref=ref["code"])
    assert friend["user_id"] != uid
    ref = client.get(f"/api/users/{uid}/referral").json()
    assert ref["referrals"] == 1 and ref["level_name"] == "Kerbside regular"


def test_dispatch_and_inbound(client, db):
    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123"})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})

    first = db.scalar(select(Reminder).order_by(Reminder.send_at))
    sent = dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    assert sent == 1
    msg = db.scalar(select(Outbox).where(Outbox.kind == "reminder"))
    assert "goes out tomorrow" in msg.body or "go out tomorrow" in msg.body
    assert msg.provider == "stub"

    r = client.post("/api/inbound", json={"phone": "+447700900123", "text": "done"})
    assert "Nice one" in r.json()["reply"]
    db.expire_all()
    assert db.get(Reminder, first.id).status == ReminderStatus.done

    r = client.post("/api/inbound", json={"phone": "+447700900123", "text": "STOP"})
    assert "off" in r.json()["reply"]
    assert db.scalar(select(Reminder).where(Reminder.status == ReminderStatus.scheduled)) is None


def test_health(client):
    j = client.get("/api/health").json()
    assert j["ok"] and j["coverage"]["councils"] > 300


def test_neighbours_share_a_round(client, db):
    """Second home on a postcode reuses the first one's calendar: no fetch, no address lookup."""
    from sqlalchemy import select
    from app.models import Property, Snapshot

    first = signup(client)
    assert first["shared"] is False and first["source"] == "demo"
    assert client.get("/api/postcode/E8%203PB").json()["round_known"] is True

    r = client.post("/api/signup/start", json={"postcode": "E8 3PB"})  # no uprn, no house
    second = r.json()
    assert second["shared"] is True and second["source"] == "neighbours"
    assert sorted(b["label"] for b in second["bins"]) == sorted(b["label"] for b in first["bins"])
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 1
    keys = {p.round_key for p in db.scalars(select(Property))}
    assert len(keys) == 1 and keys.pop().startswith("pc:")

    # "Not my days": detach, fetch own calendar, settings reseeded.
    r = client.post(f"/api/users/{second['user_id']}/own-calendar",
                    json={"uprn": "10000003", "house_number": "14"})
    assert r.status_code == 200 and r.json()["shared"] is False
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 2
    assert len({p.round_key for p in db.scalars(select(Property))}) == 2
    assert len(client.get(f"/api/users/{second['user_id']}/bins").json()) == 4
