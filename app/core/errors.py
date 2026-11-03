"""Error alerts: unexpected errors go to Sentry (sentry.io) when SENTRY_DSN is set, so a bug
after launch emails us instead of sitting in a log nobody reads.

Residents' details stay out of it: no request bodies, cookies or IP addresses, and phone numbers
and postcodes are blanked from every message before it leaves the server.
"""
import re

PHONE = re.compile(r"\+?44\s?7\d{3}\s?\d{6}|\b07\d{3}\s?\d{6}\b|\+\d{10,15}")
POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b", re.I)


def scrub(value):
    """Blank phone numbers and postcodes anywhere in a (nested) event."""
    if isinstance(value, str):
        return POSTCODE.sub("[postcode]", PHONE.sub("[phone]", value))
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(scrub(v) for v in value)
    return value


def _before_send(event, hint):
    event.pop("user", None)
    req = event.get("request") or {}
    for k in ("data", "cookies", "query_string", "env"):
        req.pop(k, None)
    return scrub(event)


def init_errors(component: str) -> bool:
    """Turn on error alerts for this process. Returns whether they're on."""
    from app.core.config import get_settings

    s = get_settings()
    if not s.sentry_dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(dsn=s.sentry_dsn, environment=s.app_env, server_name=component,
                    send_default_pii=False, max_request_body_size="never",
                    traces_sample_rate=0, before_send=_before_send,
                    before_send_transaction=lambda e, h: None)
    sentry_sdk.set_tag("component", component)
    return True
