"""Error alerts stay off without a DSN and never carry residents' details."""
from app.core import errors
from app.core.config import get_settings


def test_off_without_a_dsn():
    assert errors.init_errors("api") is False


def test_phone_numbers_and_postcodes_are_blanked():
    event = {"message": "send to +447700900123 failed for E8 3PB",
             "exception": {"values": [{"value": "07700 900123 at SW1A 1AA"}]},
             "extra": {"to": "+447700900123"}, "user": {"ip_address": "1.2.3.4"},
             "request": {"url": "https://binjamin.uk/api/signup/start", "data": {"postcode": "E8 3PB"},
                         "cookies": {"a": "b"}}}
    out = errors._before_send(event, {})
    assert out["message"] == "send to [phone] failed for [postcode]"
    assert out["exception"]["values"][0]["value"] == "[phone] at [postcode]"
    assert out["extra"]["to"] == "[phone]"
    assert "user" not in out and "data" not in out["request"] and "cookies" not in out["request"]


def test_on_with_a_dsn(monkeypatch):
    import sentry_sdk
    calls = {}
    monkeypatch.setattr(get_settings(), "sentry_dsn", "https://key@o1.ingest.de.sentry.io/1")
    monkeypatch.setattr(sentry_sdk, "init", lambda **k: calls.update(k))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda *a: None)
    assert errors.init_errors("worker") is True
    assert calls["send_default_pii"] is False and calls["max_request_body_size"] == "never"
