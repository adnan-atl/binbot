"""Calls the scraper container (app/scraper/service.py)."""
import httpx

from app.adapters.base import AdapterError, LookupInput, RawCollection
from app.adapters.registry import AdapterSpec
from app.core.config import get_settings
from app.scraper.wire import lookup_to_json, result_from_json, spec_to_json


def remote_fetch(spec: AdapterSpec, lookup: LookupInput) -> list[RawCollection]:
    s = get_settings()
    # The service enforces scrape_timeout_seconds itself; allow for queueing behind the
    # browser sessions on top of that.
    timeout = s.scrape_timeout_seconds * 3 + 15
    try:
        r = httpx.post(s.scraper_url.rstrip("/") + "/fetch",
                       json={"spec": spec_to_json(spec), "lookup": lookup_to_json(lookup)},
                       headers={"X-Scraper-Token": s.scraper_token}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise AdapterError(f"{spec.module}: scraper service unavailable: {type(e).__name__}: {e}") from e
    return result_from_json(data)
