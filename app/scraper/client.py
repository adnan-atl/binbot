"""Calls the scraper container (app/scraper/service.py)."""
import logging
import time

import httpx

from app.adapters.base import AdapterError, LookupInput, RawCollection
from app.adapters.registry import AdapterSpec
from app.core.config import get_settings
from app.scraper.wire import lookup_to_json, result_from_json, spec_to_json

log = logging.getLogger(__name__)


# A deploy recreates the scraper container: for a few seconds its name doesn't resolve and
# connections are refused. Keep trying through that instead of failing the sign-up (a
# resident was once sent to add their bins by hand because of it).
RECONNECT_FOR_SECONDS = 45
RECONNECT_EVERY = 2


def remote_fetch(spec: AdapterSpec, lookup: LookupInput) -> list[RawCollection]:
    s = get_settings()
    # The service enforces scrape_timeout_seconds itself; allow for queueing behind the
    # browser sessions on top of that.
    timeout = s.scrape_timeout_seconds * 3 + 15
    give_up = time.monotonic() + RECONNECT_FOR_SECONDS
    while True:
        try:
            r = httpx.post(s.scraper_url.rstrip("/") + "/fetch",
                           json={"spec": spec_to_json(spec), "lookup": lookup_to_json(lookup)},
                           headers={"X-Scraper-Token": s.scraper_token}, timeout=timeout)
            if r.status_code in (502, 503) and time.monotonic() < give_up:
                time.sleep(RECONNECT_EVERY)  # up but not ready yet
                continue
            r.raise_for_status()
            data = r.json()
            break
        except httpx.ConnectError as e:  # not there (restarting): try again shortly
            if time.monotonic() >= give_up:
                raise AdapterError(f"{spec.module}: scraper service unavailable: "
                                   f"{type(e).__name__}: {e}") from e
            log.info("scraper not reachable (%s): retrying", e)
            time.sleep(RECONNECT_EVERY)
        except (httpx.HTTPError, ValueError) as e:
            raise AdapterError(f"{spec.module}: scraper service unavailable: {type(e).__name__}: {e}") from e
    return result_from_json(data)
