"""The scraper container. Runs the council scrapers (third-party code reading third-party
websites) away from everything that matters: it has no database connection, no Meta or address
keys, a read-only filesystem and no published port. The api and worker post a council + home
to it and get dates back.

    uvicorn app.scraper.service:app --host 0.0.0.0 --port 8100
"""
import hmac
import logging

from fastapi import FastAPI, Header, HTTPException

from app.adapters.base import AdapterError
from app.adapters.ukbcd import UKBCDAdapter
from app.core.config import get_settings
from app.scraper.wire import error_to_json, lookup_from_json, result_to_json, spec_from_json

log = logging.getLogger(__name__)
app = FastAPI(title="binjamin scraper", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/fetch")
def fetch(body: dict, x_scraper_token: str = Header(default="")) -> dict:
    token = get_settings().scraper_token
    if token and not hmac.compare_digest(x_scraper_token, token):
        raise HTTPException(403, "bad token")
    try:
        spec, lookup = spec_from_json(body["spec"]), lookup_from_json(body["lookup"])
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(422, f"bad request: {e}") from e
    # Sync endpoint: FastAPI runs it in a thread, and the browser-slot semaphore and timeout
    # inside fetch_local now cover every caller (api and worker) in one place.
    try:
        return result_to_json(UKBCDAdapter(spec).fetch_local(lookup))
    except AdapterError as e:
        return error_to_json(e)
    except Exception as e:  # never let one bad scraper take the service down
        log.exception("scraper %s crashed", spec.module)
        return error_to_json(AdapterError(f"{spec.module}: {type(e).__name__}: {e}"))
