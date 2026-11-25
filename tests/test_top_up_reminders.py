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
