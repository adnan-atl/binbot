"""Product analytics: what residents do with Binjamin, sent to PostHog (EU cloud) when
POSTHOG_KEY is set. Off otherwise (local, tests), and never in the way: a failure here is
logged and forgotten, it never stops a reminder or a reply.

The website sends its own events (web/designs/oat.html); these are the ones only the server
sees, mostly what happens in WhatsApp. Both use the resident's user id as PostHog's distinct id,
so a visit, the sign-up and the reminders that follow line up as one person.

What goes to PostHog, and what never does:
- the user id (a random string), the event name and a few plain properties: the council, how
  they signed up, a count, a reply word, a time;
- never a phone number, postcode, address, name or anything a resident typed. Strings are run
  through the same scrubber as the error alerts as a second line of defence.
The full list of events is in docs/analytics.md.
"""
from __future__ import annotations

import atexit
import logging

from app.core.config import get_settings
from app.core.errors import scrub

log = logging.getLogger("analytics")
_client = None


def enabled() -> bool:
    return bool(get_settings().posthog_key)


def _get_client():
    global _client
    if _client is None:
        from posthog import Posthog

        s = get_settings()
        # Batched in a background thread; flushed when the process exits. No GeoIP: the server's
        # own location says nothing about the resident.
        _client = Posthog(s.posthog_key, host=s.posthog_host, disable_geoip=True)
        atexit.register(_client.shutdown)
    return _client


def _capture(event: str, distinct_id: str, properties: dict) -> None:
    _get_client().capture(event, distinct_id=distinct_id, properties=properties)


def _council(user) -> str | None:
    prop = getattr(user, "property", None)
    council = getattr(prop, "council", None) if prop else None
    return getattr(council, "name", None)


def track(user, event: str, **props) -> None:
    """Record one thing a resident did (or that happened to them). `user` is a User or a user
    id. Properties must be plain values (str, int, float, bool, None)."""
    if not enabled() or user is None:
        return
    try:
        uid = user if isinstance(user, str) else user.id
        base = {"source": "server"}
        if not isinstance(user, str):
            base["council"] = _council(user)
            base["referred"] = bool(getattr(user, "referred_by_id", None))
        clean = {k: scrub(v) for k, v in {**base, **props}.items()
                 if v is None or isinstance(v, (str, int, float, bool))}
        _capture(event, uid, clean)
    except Exception:  # never let analytics break the product
        log.warning("analytics: couldn't record %s", event, exc_info=True)
