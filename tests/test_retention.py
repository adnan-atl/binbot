"""Accounts idle for 2 years are deleted, unfinished sign-ups after 30 days, and a working
subscription never is."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import ChangeLog, Outbox, Property, Referral, Reminder, User, UserBinSetting, UserStatus
from app.services.reminders import dispatch_due
from app.services.retention import INACTIVE_DAYS, UNFINISHED_SIGNUP_DAYS, purge_inactive
from tests.test_whatsapp_settings import PHONE, say, signup, uid  # noqa: F401  (uid is a fixture)

NOW = datetime.now(timezone.utc)


def days(n):
    return NOW + timedelta(days=n)


def user(db, uid):
    db.expire_all()
    return db.get(User, uid)


def test_active_subscriber_is_kept_while_reminders_reach_them(db, uid):  # noqa: F811
    for r in db.scalars(select(Reminder).where(Reminder.user_id == uid)):
        r.send_at = days(INACTIVE_DAYS - 10)
    db.commit()
    dispatch_due(db, now_utc=days(INACTIVE_DAYS - 10))  # a reminder reaches them late on
    assert user(db, uid).last_active_at is not None
    assert purge_inactive(db, now_utc=days(INACTIVE_DAYS + 1)) == 0
    assert user(db, uid)


def test_idle_account_is_deleted_with_everything_that_identifies_them(client, db, uid):  # noqa: F811
    say(client, "can you do glass?")  # unrecognised note, scope user
    say(client, "WRONG")
    db.add(ChangeLog(scope="round", scope_id="r1", kind="wrong_day", diff={"user_id": uid}))
    db.add(ChangeLog(scope="round", scope_id="r1", kind="wrong_day", diff={"user_id": "someone-else"}))
    db.commit()
    prop_id = user(db, uid).property_id
    assert db.scalars(select(Outbox).where(Outbox.to_phone == PHONE)).all()

    later = datetime.now(timezone.utc) + timedelta(days=INACTIVE_DAYS + 1)
    assert purge_inactive(db, now_utc=later) == 1
    db.expire_all()
    assert db.get(User, uid) is None
    assert not db.scalars(select(Reminder).where(Reminder.user_id == uid)).all()
    assert not db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == uid)).all()
    assert not db.scalars(select(Outbox).where(Outbox.to_phone == PHONE)).all()
    assert not db.scalars(select(ChangeLog).where(ChangeLog.scope == "user")).all()
    votes = db.scalars(select(ChangeLog).where(ChangeLog.kind == "wrong_day")).all()
    assert [v.diff["user_id"] for v in votes] == ["someone-else"]
    assert db.get(Property, prop_id)  # the home's calendar stays for the neighbours


def test_using_it_resets_the_clock(client, db, uid):  # noqa: F811
    u = user(db, uid)
    u.last_active_at = NOW - timedelta(days=INACTIVE_DAYS - 1)
    db.commit()
    say(client, "settings")
    seen = user(db, uid).last_active_at
    assert seen.replace(tzinfo=seen.tzinfo or timezone.utc) > NOW - timedelta(minutes=1)
    assert purge_inactive(db, now_utc=days(INACTIVE_DAYS - 5)) == 0


def test_stopped_account_goes_two_years_after_stop(client, db, uid):  # noqa: F811
    say(client, "STOP")
    assert user(db, uid).status == UserStatus.stopped
    assert purge_inactive(db, now_utc=days(INACTIVE_DAYS - 1)) == 0
    assert purge_inactive(db, now_utc=days(INACTIVE_DAYS + 1)) == 1


def test_unfinished_signup_goes_after_30_days(client, db):
    pending = signup(client)["user_id"]
    assert user(db, pending).status == UserStatus.pending
    assert purge_inactive(db, now_utc=days(UNFINISHED_SIGNUP_DAYS - 1)) == 0
    assert purge_inactive(db, now_utc=days(UNFINISHED_SIGNUP_DAYS + 1)) == 1
    assert user(db, pending) is None


def test_referrals_survive_either_side_going(db, uid):  # noqa: F811
    friend = User(referral_code="friend01", referred_by_id=uid, status=UserStatus.active,
                  last_active_at=NOW)
    db.add(friend)
    db.flush()
    db.add(Referral(referrer_id=uid, referred_id=friend.id))
    db.commit()
    friend_id = friend.id
    # The friend goes idle first: the referrer keeps the credit
    f = user(db, friend_id)
    f.last_active_at = f.created_at = NOW - timedelta(days=INACTIVE_DAYS + 1)
    db.commit()
    assert purge_inactive(db) == 1
    db.expire_all()
    assert [(r.referrer_id, r.referred_id) for r in db.scalars(select(Referral))] == [(uid, None)]
    # Then the referrer: their referral rows go with them
    u = user(db, uid)
    u.last_active_at = u.created_at = NOW - timedelta(days=INACTIVE_DAYS + 1)
    db.commit()
    assert purge_inactive(db) == 1
    assert not db.scalars(select(Referral)).all()


def test_referred_friend_outlives_referrer(db, uid):  # noqa: F811
    friend = User(referral_code="friend02", referred_by_id=uid, status=UserStatus.active,
                  last_active_at=NOW)
    db.add(friend)
    u = user(db, uid)
    u.last_active_at = u.created_at = NOW - timedelta(days=INACTIVE_DAYS + 1)
    db.commit()
    fid = friend.id
    assert purge_inactive(db) == 1
    assert user(db, fid).referred_by_id is None


def test_old_sent_messages_are_deleted(db):
    db.add(Outbox(to_phone="+447700900999", body="old", kind="reminder", provider="stub",
                  created_at=NOW - timedelta(days=INACTIVE_DAYS + 1)))
    db.add(Outbox(to_phone="+447700900999", body="new", kind="reminder", provider="stub"))
    db.commit()
    purge_inactive(db)
    db.expire_all()
    assert [o.body for o in db.scalars(select(Outbox))] == ["new"]


# ---- delete on request ---------------------------------------------------------------------------

def test_delete_asks_first_then_wipes_the_account(client, db, uid):  # noqa: F811
    from app.services.reminders import DELETE_CONFIRM, DELETED
    assert say(client, "DELETE") == DELETE_CONFIRM
    assert user(db, uid)  # nothing deleted yet
    assert say(client, "menu:delete_yes") == DELETED
    db.expire_all()
    assert db.get(User, uid) is None
    assert not db.scalars(select(Outbox).where(Outbox.to_phone == PHONE)).all()
    assert not db.scalars(select(Reminder).where(Reminder.user_id == uid)).all()
    # Afterwards they're a stranger again
    assert "What's your postcode?" in say(client, "hello")


def test_delete_from_the_settings_menu_and_typed_yes(client, db, uid):  # noqa: F811
    from app.services.reminders import DELETE_CONFIRM, DELETED
    assert say(client, "menu:delete") == DELETE_CONFIRM
    assert say(client, "no thanks") != DELETED  # anything else keeps the account
    assert user(db, uid)
    assert say(client, "delete yes") == DELETED
    assert user(db, uid) is None
