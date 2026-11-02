"""POST /webhooks/meta only accepts bodies signed with the app secret (X-Hub-Signature-256)."""
import hashlib
import hmac
import json

import pytest

from app.core.config import get_settings

BODY = json.dumps({"entry": [{"changes": [{"value": {"messages": [
    {"from": "447700900999", "type": "text", "text": {"body": "hi"}}]}}]}]}).encode()


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "s3cret")
    get_settings.cache_clear()
    yield "s3cret"
    monkeypatch.delenv("META_APP_SECRET")
    get_settings.cache_clear()


def _post(client, body, sig=None):
    headers = {"Content-Type": "application/json"}
    if sig is not None:
        headers["X-Hub-Signature-256"] = sig
    return client.post("/webhooks/meta", content=body, headers=headers)


def test_signed_body_is_accepted(client, secret):
    r = _post(client, BODY, _sign(BODY, secret))
    assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize("sig", [None, "", "sha256=deadbeef", "sha1=abc"])
def test_missing_or_wrong_signature_is_rejected(client, secret, sig):
    assert _post(client, BODY, sig).status_code == 403


def test_signature_for_a_different_body_is_rejected(client, secret):
    assert _post(client, BODY + b" ", _sign(BODY, secret)).status_code == 403


def test_wrong_secret_is_rejected(client, secret):
    assert _post(client, BODY, _sign(BODY, "other")).status_code == 403


def test_no_secret_skips_the_check_for_local_testing(client):
    assert _post(client, BODY).status_code == 200
