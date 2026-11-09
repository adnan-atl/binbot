import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEMO_MODE"] = "true"
os.environ["MESSAGE_PROVIDER"] = "stub"
os.environ["RATE_LIMITS"] = "false"  # test_guard.py turns them on where it needs them
os.environ["BRAND_NAME"] = "Binny"  # tests pin a fixed name; the product default lives in config.py

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import db as dbmod  # noqa: E402
from app.core.config import get_settings  # noqa: E402
import app.models  # noqa: E402,F401
from app.main import app as fastapi_app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    get_settings.cache_clear()
    dbmod.reset_engine()
    dbmod.Base.metadata.create_all(dbmod.get_engine())
    yield
    dbmod.Base.metadata.drop_all(dbmod.get_engine())


@pytest.fixture
def client():
    return TestClient(fastapi_app)


@pytest.fixture
def db():
    s = dbmod.session_factory()()
    yield s
    s.close()


@pytest.fixture
def midday(monkeypatch):
    """Pin the reminders clock to 12:00 today. Tests that read the 'next up' message or the
    welcome otherwise fail after 7pm on the evening before a collection, when tomorrow's
    reminder time has already passed (the app is right; the test assumed daytime)."""
    from datetime import datetime as real_dt

    from app.services import reminders

    class Midday(real_dt):
        @classmethod
        def now(cls, tz=None):
            n = real_dt.now(tz)
            return n.replace(hour=12, minute=0, second=0, microsecond=0)

    monkeypatch.setattr(reminders, "datetime", Midday)
