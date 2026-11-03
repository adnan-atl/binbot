"""How long we keep people's data, and the daily job that deletes it.

- An account nobody has used for 2 years is deleted: no message or tap on WhatsApp, no visit
  to the site, and no reminder that reached them. Someone getting reminders every week counts
  as using it, so a working subscription is never deleted. One whose number stopped working
  (every send fails) or who replied STOP stops counting from then.
- A sign-up that never got as far as turning reminders on is deleted after 30 days.
- Sent messages are kept for 2 years (to answer "did I get a reminder?"), then deleted.
- Messages the bot didn't understand: 90 days (reminders.purge_unrecognised).

Deleting an account removes the phone number, settings, reminders, the messages sent to it and
any of its notes in the change log. What's left is the home's address and bin calendar, which
is shared with neighbours on the same round and no longer linked to anyone. A friend they
referred keeps their account, and they keep the credit for the friends they referred.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.models import ChangeLog, Outbox, Referral, Reminder, User, UserStatus

log = logging.getLogger(__name__)

INACTIVE_DAYS = 730
UNFINISHED_SIGNUP_DAYS = 30
OUTBOX_KEEP_DAYS = 730


def purge_inactive(db: Session, now_utc: datetime | None = None) -> int:
    """Delete accounts past their keep date. Returns how many. Run daily by the worker."""
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    idle_before = now_utc - timedelta(days=INACTIVE_DAYS)
    unfinished_before = now_utc - timedelta(days=UNFINISHED_SIGNUP_DAYS)
    users = db.scalars(select(User).where(or_(
        func.coalesce(User.last_active_at, User.created_at) < idle_before,
        (User.status == UserStatus.pending) & (User.created_at < unfinished_before),
    ))).all()
    for u in users:
        delete_account(db, u)
    gone = db.execute(delete(Outbox).where(Outbox.created_at < now_utc - timedelta(days=OUTBOX_KEEP_DAYS)))
    db.commit()
    if users or gone.rowcount:
        log.info("retention: deleted %d accounts and %d old messages", len(users), gone.rowcount)
    return len(users)


def delete_account(db: Session, user: User) -> None:
    """Remove everything that identifies this person. Doesn't commit."""
    uid = user.id
    db.execute(delete(Reminder).where(Reminder.user_id == uid))
    if user.phone_e164:
        db.execute(delete(Outbox).where(Outbox.to_phone == user.phone_e164))
    db.execute(delete(ChangeLog).where(ChangeLog.scope == "user", ChangeLog.scope_id == uid))
    # "These aren't my days" votes carry the user id in the round's log
    for row in db.scalars(select(ChangeLog).where(ChangeLog.kind == "wrong_day")):
        if (row.diff or {}).get("user_id") == uid:
            db.delete(row)
    db.execute(delete(Referral).where(Referral.referrer_id == uid))
    db.execute(update(Referral).where(Referral.referred_id == uid).values(referred_id=None))
    db.execute(update(User).where(User.referred_by_id == uid).values(referred_by_id=None))
    db.delete(user)  # bin settings go with it (cascade)
    db.flush()
