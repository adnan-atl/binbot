"""Abuse protection for the sign-up endpoints: per-IP rate limits and a Cloudflare Turnstile check.

Address lookups cost money and council websites shouldn't be hammered, so the three endpoints that
trigger them (address list, sign-up, "not my days") are limited per IP and, when Turnstile keys are
set, need a short-lived "human pass" from POST /api/human.
"""
import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque

import httpx
from fastapi import HTTPException, Request

from app.core.config import get_settings

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque] = defaultdict(deque)

PASS_SECONDS = 30 * 60  # one pass covers a whole sign-up
TOO_MANY = "Too many tries from your connection. Wait a few minutes and try again."


def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers behind Caddy, so this is the visitor's address
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, bucket: str, max_hits: int, per_seconds: int) -> None:
    """Allow max_hits per IP in a sliding window of per_seconds; 429 after that. In memory: the
    API runs as one process, and a restart forgetting the counts is fine."""
    if not get_settings().rate_limits:
        return
    key, now = (bucket, client_ip(request)), time.monotonic()
    with _lock:
        q = _hits[key]
        while q and q[0] <= now - per_seconds:
            q.popleft()
        if len(q) >= max_hits:
            raise HTTPException(429, TOO_MANY)
        q.append(now)


def reset_limits() -> None:
    with _lock:
        _hits.clear()


# ---- human check (Cloudflare Turnstile) ---------------------------------------------------------

def human_check_on() -> bool:
    return bool(get_settings().turnstile_secret)


def _sign(exp: int) -> str:
    key = ("human-pass:" + get_settings().turnstile_secret).encode()
    return hmac.new(key, str(exp).encode(), hashlib.sha256).hexdigest()[:32]


def issue_pass(now: float | None = None) -> str:
    exp = int(now or time.time()) + PASS_SECONDS
    return f"{exp}.{_sign(exp)}"


def pass_ok(value: str, now: float | None = None) -> bool:
    try:
        exp_s, sig = value.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    return exp > (now or time.time()) and hmac.compare_digest(sig, _sign(exp))


def verify_turnstile(token: str, ip: str) -> bool:
    """Ask Cloudflare whether the widget's token is good. Any error counts as a failed check."""
    try:
        r = httpx.post("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                       data={"secret": get_settings().turnstile_secret, "response": token,
                             "remoteip": ip}, timeout=10)
        return bool(r.json().get("success"))
    except (httpx.HTTPError, ValueError):
        return False


def require_human(request: Request) -> None:
    if human_check_on() and not pass_ok(request.headers.get("X-Human-Pass", "")):
        raise HTTPException(403, "Please complete the quick check and try again.")
