import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEMO_MODE"] = "true"
os.environ["MESSAGE_PROVIDER"] = "stub"

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
