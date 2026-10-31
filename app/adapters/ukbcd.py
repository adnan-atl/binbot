"""Wraps a UKBinCollectionData council module as a CalendarAdapter."""
import concurrent.futures
import json
import logging
from datetime import date, datetime

from app.adapters.base import AdapterError, LookupInput, MissingInput, RawCollection
from app.adapters.registry import AdapterSpec
from app.core.config import get_settings

log = logging.getLogger(__name__)

_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %B %Y", "%A %d %B %Y")


def parse_uk_date(s: str) -> date:
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise AdapterError(f"unparseable date: {s!r}", retryable=False)


def _run_module(spec: AdapterSpec, lookup: LookupInput) -> str:
    from uk_bin_collection.uk_bin_collection.collect_data import import_council_module

    settings = get_settings()
    if spec.needs_browser and not settings.selenium_url:
        raise AdapterError("council needs a browser; set SELENIUM_URL", retryable=False)

    # Many councils embed the UPRN in the URL (XXXXXXXX placeholder).
    url = spec.url.replace("XXXXXXXX", lookup.uprn or "")
    module = import_council_module(spec.module)
    return module.CouncilClass().template_method(
        url,
        postcode=lookup.postcode,
        paon=lookup.house_number,
        uprn=lookup.uprn,
        skip_get_url=spec.skip_get_url,
        web_driver=settings.selenium_url or None,
        headless=True,
        local_browser=False,
        dev_mode=False,
    )


class UKBCDAdapter:
    def __init__(self, spec: AdapterSpec):
        self.spec = spec
        self.name = spec.module

    def validate(self, lookup: LookupInput) -> None:
        if self.spec.needs_uprn and not lookup.uprn:
            raise MissingInput("uprn")
        if self.spec.needs_house_number and not lookup.house_number:
            raise MissingInput("house_number")

    def fetch(self, lookup: LookupInput) -> list[RawCollection]:
        self.validate(lookup)
        from app.adapters.overrides import OVERRIDES

        if self.spec.module in OVERRIDES:  # local fix for a broken upstream scraper
            return OVERRIDES[self.spec.module](lookup)
        timeout = get_settings().scrape_timeout_seconds
        # Council modules are synchronous and sometimes hang; isolate with a timeout.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_run_module, self.spec, lookup)
            try:
                out = fut.result(timeout=timeout)
            except concurrent.futures.TimeoutError as e:
                raise AdapterError(f"{self.name}: timed out after {timeout}s") from e
            except AdapterError:
                raise
            except Exception as e:  # scraper blew up: site changed, blocked, etc.
                raise AdapterError(f"{self.name}: {type(e).__name__}: {e}") from e
        return parse_output(out)


def parse_output(out: str | dict) -> list[RawCollection]:
    data = json.loads(out) if isinstance(out, str) else out
    bins = data.get("bins", []) if isinstance(data, dict) else []
    result: list[RawCollection] = []
    for b in bins:
        label = (b.get("type") or b.get("bin_type") or "").strip()
        raw_date = b.get("collectionDate") or b.get("date")
        if not label or not raw_date:
            continue
        result.append(RawCollection(label=label, date=parse_uk_date(str(raw_date))))
    if not result:
        raise AdapterError("adapter returned no collections")
    return result
