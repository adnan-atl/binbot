"""Residents change their reminder time and bins over WhatsApp: SETTINGS, TIME, MORNING,
DAY BEFORE, BINS, ON and OFF."""
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Reminder, ReminderMode, ReminderStatus, User, UserBinSetting
from app.services.reminders import parse_time
from tests.test_flow import signup

PHONE = "+447700900123"
LONDON = ZoneInfo("Europe/London")


@pytest.fixture
def uid(client):
    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123"})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


def say(client, text):
    return client.post("/api/inbound", json={"phone": PHONE, "text": text}).json()["reply"]


def scheduled(db, uid):
    db.expire_all()
    return db.scalars(select(Reminder).where(Reminder.user_id == uid,
                                             Reminder.status == ReminderStatus.scheduled)
                      .order_by(Reminder.send_at)).all()


@pytest.mark.parametrize("raw,mode,want", [
    ("4pm", ReminderMode.night_before, "16:00"),
    ("4 PM", ReminderMode.night_before, "16:00"),
    ("16:00", ReminderMode.morning_of, "16:00"),
    ("6.30am", ReminderMode.night_before, "06:30"),
    ("6:30", ReminderMode.morning_of, "06:30"),
    ("6:30", ReminderMode.night_before, "18:30"),
    ("7", ReminderMode.night_before, "19:00"),
    ("7", ReminderMode.morning_of, "07:00"),
    ("07:00", ReminderMode.night_before, "07:00"),
    ("12pm", ReminderMode.morning_of, "12:00"),
    ("12am", ReminderMode.night_before, "00:00"),
    ("12", ReminderMode.night_before, "12:00"),
])
def test_parse_time(raw, mode, want):
    assert parse_time(raw, mode) == want


@pytest.mark.parametrize("raw", ["banana", "25", "13pm", "0am", "7:75", "1630", "", "7 o'clock"])
def test_parse_time_rejects_nonsense(raw):
    assert parse_time(raw, ReminderMode.night_before) is None


def test_settings_lists_time_bins_and_commands(client, uid):
    for word in ("SETTINGS", "settings", "  Menu ", "HELP", "change"):
        reply = say(client, word)
        assert reply.startswith("Your Binny settings\nReminders: the day before at 7pm\n")
        assert "\nBins:\n1. Blue recycling bin (on)\n2. Black general waste bin (on)\n" in reply
        assert reply.endswith("To change them, send:\nTIME 6pm for a new time\n"
                              "MORNING or DAY BEFORE to switch day\n"
                              "OFF 2 or ON 2 to turn a bin off or on")


def test_time_changes_the_time_and_rebuilds_reminders(client, db, uid):
    before = len(scheduled(db, uid))
    reply = say(client, "TIME   4pm")
    assert reply.startswith("Done. I'll message you the day before each collection at 4pm.")
    assert " Next: " in reply and reply.endswith(".")
    user = db.get(User, uid)
    assert user.reminder_time == "16:00" and user.reminder_mode == ReminderMode.night_before
    rows = scheduled(db, uid)
    assert rows and len(rows) >= before - 1  # a send time that has just passed today drops out
    for r in rows:
        local = r.send_at.astimezone(LONDON)
        assert (local.hour, local.minute) == (16, 0)
        assert local.date() == r.collection_date - timedelta(days=1)
    # Next line names the first rebuilt reminder
    first = rows[0]
    assert reply.endswith(f"on {first.collection_date.strftime('%a %-d %b')}.")

    assert say(client, "time 18:45").startswith("Done. I'll message you the day before each collection at 6:45pm.")
    assert say(client, "Time 7").startswith("Done. I'll message you the day before each collection at 7pm.")
    assert db.get(User, uid).reminder_time == "19:00"


def test_bad_time_is_refused_with_an_example(client, db, uid):
    for text in ("TIME banana", "TIME", "time 25", "time 9:99"):
        assert say(client, text) == "I didn't catch that time. Try TIME 7pm or TIME 18:30."
    db.expire_all()
    assert db.get(User, uid).reminder_time == "19:00"


def test_morning_and_day_before_switch_mode(client, db, uid):
    reply = say(client, "MORNING")
    assert reply.startswith("Done. I'll message you on the morning of each collection at 6:30am.")
    user = db.get(User, uid)
    assert user.reminder_mode == ReminderMode.morning_of and user.reminder_time == "06:30"
    for r in scheduled(db, uid):
        local = r.send_at.astimezone(LONDON)
        assert local.date() == r.collection_date and (local.hour, local.minute) == (6, 30)

    # A bare hour now means the morning
    assert say(client, "TIME 7").startswith("Done. I'll message you on the morning of each collection at 7am.")
    assert say(client, "morning 6am").startswith("Done. I'll message you on the morning of each collection at 6am.")

    reply = say(client, "Day  Before")
    assert reply.startswith("Done. I'll message you the day before each collection at 7pm.")
    db.expire_all()
    assert db.get(User, uid).reminder_mode == ReminderMode.night_before
    assert say(client, "evening 8.15pm").startswith("Done. I'll message you the day before each collection at 8:15pm.")
    assert say(client, "NIGHT 6").startswith("Done. I'll message you the day before each collection at 6pm.")
    assert say(client, "day before 9").startswith("Done. I'll message you the day before each collection at 9pm.")
    assert say(client, "night 25") == "I didn't catch that time. Try DAY BEFORE 7pm."
    assert say(client, "morning banana") == "I didn't catch that time. Try MORNING 6:30am."


def test_bins_lists_numbered_bins(client, uid):
    assert say(client, "bins") == ("Your bins:\n1. Blue recycling bin (on)\n2. Black general waste bin (on)\n"
                                   "3. Food waste caddy (on)\n4. Green garden waste bin (on)\n"
                                   "Send OFF 2 or ON 2 to change one.")


def test_off_and_on_by_number_and_name(client, db, uid):
    garden = db.scalar(select(UserBinSetting).where(UserBinSetting.user_id == uid,
                                                    UserBinSetting.label == "Green garden waste bin"))
    assert say(client, "OFF 4") == ("Done. Green garden waste bin is off, so I won't remind you about it. "
                                    "Send ON 4 to turn it back on.")
    db.expire_all()
    assert db.get(UserBinSetting, garden.id).enabled is False
    assert not any(b["label"] == "Green garden waste bin" for r in scheduled(db, uid) for b in r.bins)
    assert "4. Green garden waste bin (off)" in say(client, "BINS")
    assert say(client, "off garden") == "Green garden waste bin is already off."

    reply = say(client, "on Garden")
    assert reply.startswith("Done. Green garden waste bin is back on. Next: ")
    assert "Green garden waste bin" in reply.split("Next: ")[1]
    assert any(b["label"] == "Green garden waste bin" for r in scheduled(db, uid) for b in r.bins)

    assert say(client, "off food") == ("Done. Food waste caddy is off, so I won't remind you about it. "
                                       "Send ON 3 to turn it back on.")
    assert say(client, "on 1") == "Blue recycling bin is already on."
    assert say(client, "OFF") == "Send OFF with a bin number, like OFF 2. Send BINS to see the list."
    assert say(client, "off 9") == "I couldn't find that bin. Send BINS to see the list."
    assert say(client, "off kitchen sink") == "I couldn't find that bin. Send BINS to see the list."
    assert say(client, "off bin").startswith("Which one? Send OFF with its number:\n1. Blue recycling bin")


def test_turning_off_the_last_bin_is_allowed_but_explained(client, db, uid):
    for n in (1, 2, 3):
        say(client, f"off {n}")
    assert say(client, "off 4") == ("Done. Green garden waste bin is off. That was your last bin, so you'll "
                                    "get no reminders until you turn one on (ON 4).")
    assert scheduled(db, uid) == []
    assert say(client, "on 2").startswith("Done. Black general waste bin is back on. Next: Black general waste bin on ")
    assert scheduled(db, uid)


def test_time_change_while_stopped_says_reminders_are_off(client, db, uid):
    assert "off" in say(client, "STOP")
    reply = say(client, "time 6pm")
    assert reply == ("Done. I'll message you the day before each collection at 6pm. "
                     "Reminders are off at the moment. Reply START to turn them back on.")
    assert scheduled(db, uid) == []
    assert "(off, reply START to turn them on)" in say(client, "settings")
    say(client, "START")
    rows = scheduled(db, uid)
    assert rows and all(r.send_at.astimezone(LONDON).hour == 18 for r in rows)


def test_fallback_points_to_settings(client, uid):
    assert say(client, "hello") == ("Reply DONE, SNOOZE, WRONG or STOP. "
                                    "Send SETTINGS to change your reminder time or bins.")
    # DONE with nothing sent yet falls back too
    assert say(client, "done").startswith("Reply DONE, SNOOZE, WRONG or STOP.")


def test_faq_points_to_settings(client):
    page = client.get("/").text
    assert "<p>Yes. Message Binny SETTINGS on WhatsApp to change your reminder time or turn bins on and off.</p>" in page
    assert "more options are coming" not in page
