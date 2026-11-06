"""A reminder that fails to send is tried again (10 then 30 minutes later); if it still fails,
it's given up on and the team gets one Slack alert with the error."""
from datetime import timedelta

from sqlalchemy import select

from app.models import Outbox, Reminder, ReminderStatus, User
from app.providers.messaging import SendResult
from app.services import inbox, reminders
from app.services.reminders import dispatch_due, rebuild_reminders
from tests.test_share_nudge import _on

ERR = ('{"error":{"message":"(#132001) Template name does not exist in the translation","code":132001,'
       '"error_data":{"messaging_product":"whatsapp","details":"template name (bin_reminder) does not exist in en_GB"}}}')


class Flaky:
    """A WhatsApp that fails the first `fails` sends."""
    name = "meta"

    def __init__(self, fails):
        self.fails, self.calls = fails, 0

    def send_template(self, to_phone, name, lang, params, buttons):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError(ERR)
        return SendResult(ok=True, message_id="wamid.ok")


def _setup(client, db, monkeypatch, fails):
    from app.providers import messaging
    uid = _on(client)  # signed up on the test WhatsApp; the flaky one is only for reminders
    monkeypatch.setenv("META_REMINDER_TEMPLATE", "bin_reminder")
    from app.core.config import get_settings
    get_settings.cache_clear()
    p = Flaky(fails)
    monkeypatch.setattr(messaging, "get_provider", lambda: p)
    alerts = []
    monkeypatch.setattr(inbox, "alert_failed_reminders", lambda db, f: alerts.append(f))
    db.expire_all()
    r = db.scalar(select(Reminder).where(Reminder.user_id == uid).order_by(Reminder.send_at))
    return uid, r, alerts


def _run(db, at):
    db.expire_all()
    return dispatch_due(db, now_utc=at)


def test_a_blip_is_retried_and_the_reminder_still_arrives(client, db, monkeypatch):
    uid, r, alerts = _setup(client, db, monkeypatch, fails=1)
    t0 = r.send_at + timedelta(seconds=30)
    assert _run(db, t0) == 0
    db.refresh(r)
    assert r.status == ReminderStatus.scheduled and r.attempts == 1
    assert r.send_at == t0 + timedelta(minutes=10)
    assert _run(db, t0 + timedelta(minutes=5)) == 0  # not yet
    assert _run(db, t0 + timedelta(minutes=10)) == 1
    db.refresh(r)
    assert r.status == ReminderStatus.sent and not alerts


def test_still_failing_after_retries_alerts_once(client, db, monkeypatch):
    uid, r, alerts = _setup(client, db, monkeypatch, fails=99)
    t = r.send_at + timedelta(seconds=30)
    _run(db, t)
    _run(db, t + timedelta(minutes=10))
    db.refresh(r)
    assert r.status == ReminderStatus.scheduled and r.attempts == 2
    _run(db, t + timedelta(minutes=40))
    db.refresh(r)
    assert r.status == ReminderStatus.failed and r.attempts == 3
    assert len(alerts) == 1 and alerts[0][0][0].id == r.id and "132001" in alerts[0][0][1]
    _run(db, t + timedelta(hours=2))
    assert len(alerts) == 1


def test_no_retry_once_its_too_late(client, db, monkeypatch):
    uid, r, alerts = _setup(client, db, monkeypatch, fails=99)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    late = datetime.combine(r.collection_date, reminders.RETRY_UNTIL, ZoneInfo("Europe/London")) - timedelta(minutes=5)
    db.refresh(r)
    r.send_at = late
    db.commit()
    _run(db, late)
    db.refresh(r)
    assert r.status == ReminderStatus.failed and len(alerts) == 1


def test_a_rebuild_keeps_a_reminder_mid_retry(client, db, monkeypatch):
    uid, r, alerts = _setup(client, db, monkeypatch, fails=1)
    t0 = r.send_at + timedelta(seconds=30)
    _run(db, t0)
    db.expire_all()
    rebuild_reminders(db, db.get(User, uid))
    db.commit()
    same_day = db.scalars(select(Reminder).where(Reminder.user_id == uid,
                                                 Reminder.collection_date == r.collection_date)).all()
    assert len(same_day) == 1 and same_day[0].attempts == 1


def test_alert_message(db, monkeypatch, client):
    from app.core.config import get_settings
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_ALERTS_CHANNEL", "binjamin-inbox")
    get_settings.cache_clear()
    posts = []
    monkeypatch.setattr(inbox, "_api", lambda method, **b: posts.append(b) or {"ok": True})
    uid = _on(client)
    db.expire_all()
    r = db.scalar(select(Reminder).where(Reminder.user_id == uid))
    inbox.alert_failed_reminders(db, [(r, ERR)])
    text = posts[-1]["text"]
    assert "1 reminder didn't send" in text and "<@U0C2FMB5RHD>" in text
    assert "…0123" in text and "+447700900123" not in text
    assert "(#132001) Template name does not exist in the translation: template name (bin_reminder) does not exist in en_GB" in text
