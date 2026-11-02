"""Messages the bot doesn't understand are kept (to see what people ask for), then deleted
after UNRECOGNISED_KEEP_DAYS."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import ChangeLog
from app.services.reminders import UNRECOGNISED_KEEP_DAYS, purge_unrecognised
from tests.test_whatsapp_settings import say, uid  # noqa: F401  (uid is a fixture)


def kept(db):
    db.expire_all()
    return db.scalars(select(ChangeLog).where(ChangeLog.kind == "unrecognised")).all()


def test_messages_we_dont_understand_are_kept_then_purged(client, db, uid):  # noqa: F811
    say(client, "can you remind me about the glass box?")
    say(client, "settings")  # understood: not kept
    assert [(r.scope, r.scope_id, r.diff) for r in kept(db)] == [
        ("user", uid, {"text": "can you remind me about the glass box?"})]
    assert purge_unrecognised(db) == 0
    later = datetime.now(timezone.utc) + timedelta(days=UNRECOGNISED_KEEP_DAYS + 1)
    assert purge_unrecognised(db, now_utc=later) == 1
    assert kept(db) == []
