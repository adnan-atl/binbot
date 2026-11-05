"""Residents change their reminder time and bins over WhatsApp by tapping: a Settings menu, a
time list and a bins list. Typed shortcuts (TIME 4pm, MORNING, DAY BEFORE, "turn off garden")
still work."""
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Outbox, Reminder, ReminderMode, ReminderStatus, User, UserBinSetting
from app.providers import messaging
from app.services.reminders import parse_time
from tests.test_flow import signup

PHONE = "+447700900123"
LONDON = ZoneInfo("Europe/London")
BINS = ["Blue recycling bin", "Black general waste bin", "Food waste caddy", "Green garden waste bin"]


@pytest.fixture
def uid(client):
    uid = signup(client)["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123"})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    return uid


def say(client, text):
    return client.post("/api/inbound", json={"phone": PHONE, "text": text}).json()["reply"]


def last_sent(db):
    """The readable outbox copy of the last reply: text plus its buttons or list rows."""
    db.expire_all()
    return db.scalars(select(Outbox).where(Outbox.kind == "reply").order_by(Outbox.created_at,
                                                                          Outbox.id)).all()[-1].body


def scheduled(db, uid):
    db.expire_all()
    return db.scalars(select(Reminder).where(Reminder.user_id == uid,
                                             Reminder.status == ReminderStatus.scheduled)
                      .order_by(Reminder.send_at)).all()


def setting(db, uid, label):
    db.expire_all()
    return db.scalar(select(UserBinSetting).where(UserBinSetting.user_id == uid,
                                                  UserBinSetting.label == label))


# ---- times ------------------------------------------------------------------------------------

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


MENU = ("\nSettings:\n- Reminder time (When I message you the evening before)\n- My bins (Turn bins on or off)\n"
        "- Wrong collection day (Tell us your days don't match)\n- {}\n"
        "- Delete my data (Remove your account for good)")


# ---- settings menu ------------------------------------------------------------------------------

def test_settings_menu_from_a_word_or_the_reminder_button(client, db, uid):
    for word in ("SETTINGS", "settings", "  Menu ", "HELP", "change", "menu:settings"):
        reply = say(client, word)
        assert reply == ("Your reminders: the evening before at 7pm.\n"
                         f"Bins on: {', '.join(BINS)}.\nWhat would you like to change?")
        assert last_sent(db).endswith(MENU.format("Pause reminders"))


def test_menu_shows_turn_on_when_paused(client, db, uid):
    say(client, "STOP")
    assert say(client, "settings").startswith("Your reminders: the evening before at 7pm (paused).")
    assert last_sent(db).endswith(MENU.format("Turn reminders on"))


# ---- reminder time ------------------------------------------------------------------------------

def test_time_list_marks_the_current_time(client, db, uid):
    assert say(client, "menu:time") == "Pick a time for your reminders."
    assert last_sent(db) == ("Pick a time for your reminders.\nEvening before:\n- 4pm\n- 5pm\n- 6pm\n"
                             "- 7pm (Current)\n- 8pm\n- 9pm")


def test_tapping_a_time_saves_it_and_rebuilds_reminders(client, db, uid):
    reply = say(client, "time:16:00")
    assert reply.startswith("Done. I'll remind you the evening before at 4pm. Next: ")
    user = db.get(User, uid)
    assert user.reminder_time == "16:00" and user.reminder_mode == ReminderMode.night_before
    rows = scheduled(db, uid)
    assert rows
    for r in rows:
        local = r.send_at.astimezone(LONDON)
        assert (local.hour, local.minute) == (16, 0) and local.date() == r.collection_date - timedelta(days=1)
    assert reply.endswith(f"on {rows[0].collection_date.strftime('%a %-d %b')}.")

    assert say(client, "time_m:06:30").startswith("Done. I'll remind you on the morning of collection at 6:30am.")
    db.expire_all()
    assert db.get(User, uid).reminder_mode == ReminderMode.morning_of
    for r in scheduled(db, uid):
        local = r.send_at.astimezone(LONDON)
        assert local.date() == r.collection_date and (local.hour, local.minute) == (6, 30)


def test_a_time_that_isnt_offered_shows_the_list_again(client, db, uid):
    assert say(client, "time:03:00") == "Pick a time for your reminders."
    db.expire_all()
    assert db.get(User, uid).reminder_time == "19:00"


def test_typed_time_shortcuts_still_work(client, db, uid):
    assert say(client, "TIME   4pm").startswith("Done. I'll remind you the evening before at 4pm.")
    assert say(client, "time 18:45").startswith("Done. I'll remind you the evening before at 6:45pm.")
    assert say(client, "MORNING").startswith("Done. I'll remind you on the morning of collection at 6:30am.")
    assert say(client, "TIME 7").startswith("Done. I'll remind you on the morning of collection at 7am.")
    assert say(client, "Day  Before").startswith("Done. I'll remind you the evening before at 7pm.")
    assert say(client, "evening 8.15pm").startswith("Done. I'll remind you the evening before at 8:15pm.")
    assert say(client, "night 25") == "I didn't catch that time. Try EVENING 7pm."
    assert say(client, "TIME banana").startswith("I didn't catch that time.")
    assert "- 4pm" in last_sent(db)  # with the list to pick from
    db.expire_all()
    assert db.get(User, uid).reminder_time == "20:15"


# ---- bins ---------------------------------------------------------------------------------------

def test_bins_list_has_a_row_per_bin(client, db, uid):
    assert say(client, "menu:bins") == "Tap a bin to turn its reminders on or off."
    assert last_sent(db) == ("Tap a bin to turn its reminders on or off.\nYour bins:\n"
                             + "\n".join(f"- {b} (On, tap to turn off)" for b in BINS))


def test_tapping_a_bin_toggles_it(client, db, uid):
    garden = setting(db, uid, "Green garden waste bin")
    assert say(client, f"bin:{garden.id}") == "Green garden waste bin is off. I won't remind you about it."
    assert last_sent(db).endswith("\n[My bins] [Done]")
    assert setting(db, uid, "Green garden waste bin").enabled is False
    assert not any(b["label"] == "Green garden waste bin" for r in scheduled(db, uid) for b in r.bins)
    say(client, "menu:bins")
    assert "- Green garden waste bin (Off, tap to turn on)" in last_sent(db)
    assert say(client, f"bin:{garden.id}").startswith("Green garden waste bin is back on. Next: ")
    assert any(b["label"] == "Green garden waste bin" for r in scheduled(db, uid) for b in r.bins)
    assert say(client, "menu:done") == "All set."


def test_someone_elses_bin_id_is_refused(client, db, uid):
    other = signup(client)["user_id"]
    theirs = db.scalar(select(UserBinSetting).where(UserBinSetting.user_id == other))
    assert say(client, f"bin:{theirs.id}") == "Tap a bin to turn its reminders on or off."
    assert say(client, "bin:nonsense") == "Tap a bin to turn its reminders on or off."
    db.expire_all()
    assert db.get(UserBinSetting, theirs.id).enabled is True


def test_turning_off_the_last_bin_is_allowed_but_explained(client, db, uid):
    for label in BINS[:3]:
        say(client, f"bin:{setting(db, uid, label).id}")
    assert say(client, f"bin:{setting(db, uid, BINS[3]).id}") == (
        "Green garden waste bin is off. That was your last bin, so you'll get no reminders until "
        "you turn one back on.")
    assert scheduled(db, uid) == []


@pytest.mark.parametrize("phrase", ["turn off garden", "Turn off the garden bin", "no garden",
                                    "stop garden", "switch garden off", "OFF garden"])
def test_plain_phrases_turn_a_bin_off(client, db, uid, phrase):
    assert say(client, phrase) == "Green garden waste bin is off. I won't remind you about it."
    assert setting(db, uid, "Green garden waste bin").enabled is False
    assert say(client, "turn on garden").startswith("Green garden waste bin is back on.")


def test_phrase_matching_several_bins_shows_the_list(client, db, uid):
    assert say(client, "turn off bin") == "Tap a bin to turn its reminders on or off."
    assert say(client, "turn off the kitchen sink") == "Tap a bin to turn its reminders on or off."
    assert all(setting(db, uid, b).enabled for b in BINS)


def test_other_no_or_stop_phrases_are_not_about_bins(client, db, uid):
    assert say(client, "no thanks").startswith("I'm afraid that's beyond my bin grade.")
    assert say(client, "stop please").startswith("Reminders are off.")


# ---- pause --------------------------------------------------------------------------------------

def test_pause_and_resume_by_tapping(client, db, uid):
    assert say(client, "menu:pause") == ("Reminders are off. Reply START or tap Turn reminders on "
                                         "when you want them back.")
    assert last_sent(db).endswith("[Turn reminders on]")
    assert db.get(User, uid).status.value == "stopped" and scheduled(db, uid) == []
    assert say(client, "time:18:00").endswith("Reminders are paused at the moment. Reply START to turn them back on.")
    assert say(client, "menu:resume").startswith("Reminders are back on.")
    rows = scheduled(db, uid)
    assert rows and all(r.send_at.astimezone(LONDON).hour == 18 for r in rows)


# ---- fallback, copy -----------------------------------------------------------------------------

def test_fallback_offers_the_settings_button(client, db, uid):
    assert say(client, "hello") == ("I'm afraid that's beyond my bin grade. I'm just here for the "
                                    "rubbish! Tap Settings to change your bins or reminder time.")
    assert last_sent(db).endswith("\n[Settings]")
    # the two lines take turns
    assert say(client, "done").startswith("I'm just a humble bin butler. Rubbish is my speciality!")
    assert say(client, "what's the weather").startswith("I'm afraid that's beyond my bin grade.")


def test_no_old_off_on_copy_anywhere(client, db, uid):
    for text in ("settings", "menu:bins", "hello", f"bin:{setting(db, uid, BINS[0]).id}"):
        say(client, text)
        assert "OFF 2" not in last_sent(db) and "ON 2" not in last_sent(db) and "BINS" not in last_sent(db)


def test_faq_points_to_settings(client):
    page = client.get("/").text
    assert ('<p>Yes. Tap Settings on any reminder, or message Binny "settings", and pick a new time '
            'or turn bins on and off.</p>') in page


# ---- Meta payloads and webhook routing ----------------------------------------------------------

class _Ok:
    status_code = 200

    def json(self):
        return {"messages": [{"id": "wamid.1"}]}


@pytest.fixture
def meta(monkeypatch):
    calls = []
    for k, v in {"MESSAGE_PROVIDER": "meta", "META_TOKEN": "t", "META_PHONE_NUMBER_ID": "1"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr(messaging.httpx, "post", lambda url, headers, json, timeout: calls.append(json) or _Ok())
    yield calls
    get_settings.cache_clear()


def test_menu_goes_out_as_an_interactive_list(client, db, uid, meta):
    say(client, "settings")
    i = meta[-1]["interactive"]
    assert i["type"] == "list" and i["action"]["button"] == "Settings"
    assert [r["id"] for r in i["action"]["sections"][0]["rows"]] == [
        "menu:time", "menu:bins", "menu:wrong", "menu:pause", "menu:delete"]


def test_wrong_collection_day_in_the_menu_is_a_wrong_vote(client, db, uid):
    from app.services.reminders import dispatch_due
    first = scheduled(db, uid)[0]
    dispatch_due(db, now_utc=first.send_at + timedelta(minutes=1))
    assert say(client, "menu:wrong").startswith("Thanks, noted.")
    db.expire_all()
    assert db.get(Reminder, first.id).reply == "wrong"


def test_time_and_bins_go_out_as_interactive_lists(client, db, uid, meta):
    say(client, "menu:time")
    i = meta[-1]["interactive"]
    assert i["type"] == "list" and i["action"]["button"] == "Pick a time"
    assert [s["title"] for s in i["action"]["sections"]] == ["Evening before"]
    assert i["action"]["sections"][0]["rows"][3] == {"id": "time:19:00", "title": "7pm", "description": "Current"}
    say(client, "menu:bins")
    rows = meta[-1]["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 4 and all(r["id"].startswith("bin:") and len(r["title"]) <= 24 for r in rows)


def test_list_limits_are_enforced(meta):
    p = messaging.MetaProvider()
    p.send_list("+447700900123", "b", "A very long button label here",
                [("S" * 30, [(f"r{i}", "T" * 30, "D" * 80) for i in range(12)])])
    a = meta[-1]["interactive"]["action"]
    rows = a["sections"][0]["rows"]
    assert len(a["button"]) <= 20 and len(a["sections"][0]["title"]) <= 24
    assert len(rows) == 10 and all(len(r["title"]) <= 24 and len(r["description"]) <= 72 for r in rows)
    p.send_buttons("+447700900123", "b", [("a", "X" * 30), ("b", "B"), ("c", "C"), ("d", "D")])
    assert [len(b["reply"]["title"]) for b in meta[-1]["interactive"]["action"]["buttons"]] == [20, 1, 1]


def _hook(client, message):
    return client.post("/webhooks/meta", json={"entry": [{"changes": [{"value": {"messages": [
        {"from": "447700900123", **message}]}}]}]})


def test_webhook_routes_button_and_list_taps(client, db, uid):
    _hook(client, {"type": "interactive", "interactive": {
        "type": "list_reply", "list_reply": {"id": "time:20:00", "title": "8pm"}}})
    db.expire_all()
    assert db.get(User, uid).reminder_time == "20:00"
    garden = setting(db, uid, "Green garden waste bin")
    _hook(client, {"type": "interactive", "interactive": {
        "type": "list_reply", "list_reply": {"id": f"bin:{garden.id}", "title": "Green garden waste"}}})
    assert setting(db, uid, "Green garden waste bin").enabled is False
    _hook(client, {"type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": "menu:pause", "title": "Pause reminders"}}})
    db.expire_all()
    assert db.get(User, uid).status.value == "stopped"
    _hook(client, {"type": "button", "button": {"text": "Settings", "payload": "menu:settings"}})
    assert last_sent(db).startswith("Your reminders: the evening before at 8pm (paused).")
