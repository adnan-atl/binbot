"""Reminders mustn't run out: a collection that appears after sign-up (the council publishing
further ahead) gets a reminder from the hourly top-up, without touching ones already booked."""
from datetime import timedelta

from sqlalchemy import select

from app.models import Reminder
from app.services.reminders import top_up_reminders
from tests.test_share_nudge import _on


def test_top_up_books_missing_reminders_only(client, db):
    uid = _on(client)
    db.expire_all()
    rows = db.scalars(select(Reminder).where(Reminder.user_id == uid)).all()
    assert rows
    keep = rows[0].id
    later = rows[1:]
    for r in later:
        db.delete(r)  # as if the calendar only reached the first collection when they joined
    db.commit()
    assert top_up_reminders(db) == len(later)
    db.expire_all()
    ids = {r.id for r in db.scalars(select(Reminder).where(Reminder.user_id == uid))}
    assert keep in ids and len(ids) == len(rows)
    assert top_up_reminders(db) == 0  # nothing left to book


def test_watchdog_flags_gaps_stuck_and_dry(client, db, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.models import ReminderStatus, User
    from app.services import inbox, reminders

    sent = []
    monkeypatch.setattr(inbox, "alert_reminder_watchdog", lambda db, g, s, d: sent.append((len(g), len(s), len(d))))
    reminders._watch_last.clear()
    uid = _on(client)
    db.expire_all()
    first = db.scalars(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at)).first()
    when = first.send_at - timedelta(hours=1)  # an hour before the first reminder is due
    assert reminders.reminder_watchdog(db, now_utc=when) == {"gaps": 0, "stuck": 0, "dry": 0}
    assert sent == []
    db.delete(first)
    db.commit()
    found = reminders.reminder_watchdog(db, now_utc=when)
    assert found["gaps"] == 1 and sent == [(1, 0, 0)]
    reminders.reminder_watchdog(db, now_utc=when + timedelta(minutes=30))
    assert len(sent) == 1  # same problem: not repeated within 6 hours
    top_up_reminders(db)
    db.expire_all()
    r = db.scalars(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at)).first()
    late = r.send_at + timedelta(minutes=30)  # booked, due, never sent
    found = reminders.reminder_watchdog(db, now_utc=late)
    assert found["stuck"] == 1 and r.status == ReminderStatus.scheduled
