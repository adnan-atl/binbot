"""Local replacements for UKBinCollectionData scrapers that are broken upstream.

When a council's library module breaks (site changed, edge case it never handled), fix it
here instead of waiting for a library release. Key = library module name. Remove the
entry once upstream catches up; the health check will tell you if the library version
starts working again.
"""
import re
from datetime import date, timedelta

import httpx
from bs4 import BeautifulSoup

from app.adapters.base import AdapterError, LookupInput, RawCollection

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
WEEKDAYS = {d: i for i, d in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
WEEKS_AHEAD = 8  # weekly rounds: emit enough dates for describe_rule() to see the pattern


def _upcoming(day_name: str, start: date, weeks: int = WEEKS_AHEAD) -> list[date]:
    wd = WEEKDAYS.get(day_name.strip().lower())
    if wd is None:
        raise AdapterError(f"unknown weekday {day_name!r}", retryable=False)
    first = start + timedelta(days=(wd - start.weekday()) % 7)
    return [first + timedelta(weeks=i) for i in range(weeks)]


def _split_days(text: str) -> list[str]:
    """'Friday and Tuesday' / 'Monday, Thursday' / 'Tuesday' -> list of day names."""
    return [p for p in re.split(r"\s*(?:,|and|&)\s*", text.strip()) if p]


# ---- Hammersmith & Fulham ---------------------------------------------------------
# Upstream (LondonBoroughHammersmithandFulham) assumes one day per bin; the council
# lists "Friday and Tuesday - Recycling" for twice-weekly rounds and the scraper raises
# ValueError. Same page, same parsing, but each listed day becomes its own weekly series.

def hammersmith_and_fulham(lookup: LookupInput, *, html: str | None = None,
                           today: date | None = None) -> list[RawCollection]:
    today = today or date.today()
    if html is None:
        pc = lookup.postcode.replace(" ", "")
        url = f"https://www.lbhf.gov.uk/bin-recycling-day/results?postcode={pc}"
        try:
            r = httpx.get(url, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise AdapterError(f"lbhf.gov.uk: {e}") from e
        html = r.text
    soup = BeautifulSoup(html, "html.parser")
    results = soup.find("div", {"class": "nearest-search-results"})
    links = results.find("ol").find_all("a") if results and results.find("ol") else []
    if not links:
        raise AdapterError("lbhf.gov.uk: no results for that postcode", retryable=False)

    out: list[RawCollection] = []
    for a in links:
        text = a.get_text(" ", strip=True)
        if " - " not in text:
            continue
        days, label = text.split(" - ", 1)
        for day in _split_days(days):
            out += [RawCollection(label=label.strip(), date=d) for d in _upcoming(day, today)]
    if not out:
        raise AdapterError("lbhf.gov.uk: page layout changed", retryable=False)
    return out


OVERRIDES = {
    "LondonBoroughHammersmithandFulham": hammersmith_and_fulham,
}

# Same weakness upstream, not yet reproduced against a live page: LondonBoroughLewisham,
# NewForestCouncil and ShetlandIslandsCouncil also read a single weekday name per bin.
# Lewisham's regex would silently keep only the first of "Tuesday and Friday". If the
# health check flags one of them, port it here the same way.
