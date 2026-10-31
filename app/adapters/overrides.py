"""Local replacements for UKBinCollectionData scrapers that are broken upstream.

When a council's library module breaks (site changed, edge case it never handled), fix it
here instead of waiting for a library release. Key = library module name. Remove the
entry once upstream catches up; the health check will tell you if the library version
starts working again.
"""
import re
from datetime import date, datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from app.adapters.base import (
    AdapterError,
    LookupInput,
    MissingInput,
    NoCollectionsForAddress,
    RawCollection,
)

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


# ---- Glasgow City ---------------------------------------------------------------------
# Upstream (GlasgowCityCouncil) keeps only "Your next Blue Bin day is Today" and throws
# away the rest of the page, which also says "Blue Bins are emptied every 14 days" and
# shows a month calendar. Read the legend (next date + period) and project it forward.

_DATE_FORMATS = ("%A, %d %B %Y", "%d/%m/%Y", "%A %d %B %Y", "%d %B %Y")


def _parse_glasgow_date(text: str, today: date) -> date | None:
    t = text.strip().rstrip(".")
    if t.lower() == "today":
        return today
    if t.lower() == "tomorrow":
        return today + timedelta(days=1)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def glasgow(lookup: LookupInput, *, html: str | None = None, today: date | None = None,
            horizon_days: int = 90) -> list[RawCollection]:
    today = today or date.today()
    if not lookup.uprn:
        raise MissingInput("uprn")
    if html is None:
        url = ("https://onlineservices.glasgow.gov.uk/forms/refuseandrecyclingcalendar/"
               f"CollectionsCalendar.aspx?UPRN={lookup.uprn}")
        try:
            r = httpx.get(url, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise AdapterError(f"glasgow.gov.uk: {e}") from e
        html = r.text
    soup = BeautifulSoup(html, "html.parser")
    out: list[RawCollection] = []

    # 1. Legend: "Your next Blue Bin day is Today. Blue Bins are emptied every 14 days."
    for p in soup.select("fieldset li p"):
        text = p.get_text(" ", strip=True)
        m = re.search(r"Your next (.+?) day is (.+?)\.\s*.*?every (\d+) days", text, re.I)
        if not m:
            continue
        label, when, every = m.group(1).strip(), m.group(2), int(m.group(3))
        nxt = _parse_glasgow_date(when, today)
        if nxt is None:
            continue
        d = nxt
        while d <= today + timedelta(days=horizon_days):
            out.append(RawCollection(label=label, date=d))
            d += timedelta(days=every)

    # 2. Month calendar: dated cells with a bin image. Adds past dates and catches
    #    one-off moves the legend's arithmetic would miss.
    for td in soup.select("table.Calendar td[title]"):
        title = td["title"].replace("today is ", "")
        d = _parse_glasgow_date(title, today)
        if d is None:
            continue
        for img in td.find_all("img"):
            mt = re.match(r"(\w+) bins", img.get("title", ""), re.I)
            if mt:
                out.append(RawCollection(label=f"{mt.group(1).title()} Bin", date=d))

    if not out:
        legend = soup.select_one("fieldset")
        calendar = soup.select_one("table.Calendar")
        if legend is not None or calendar is not None:
            # The calendar page rendered, there just are no bins on it for this UPRN.
            said = (legend.get_text(" ", strip=True) if legend else "")[:160]
            raise NoCollectionsForAddress(
                f"glasgow.gov.uk lists no collections for UPRN {lookup.uprn}: {said!r}")
        title = soup.title.get_text(strip=True) if soup.title else ""
        raise AdapterError(f"glasgow.gov.uk: page layout changed (title={title!r})",
                           retryable=False)
    return sorted(set(out), key=lambda c: (c.date, c.label))


OVERRIDES = {
    "LondonBoroughHammersmithandFulham": hammersmith_and_fulham,
    "GlasgowCityCouncil": glasgow,
}

# Same weakness upstream, not yet reproduced against a live page: LondonBoroughLewisham,
# NewForestCouncil and ShetlandIslandsCouncil also read a single weekday name per bin.
# Lewisham's regex would silently keep only the first of "Tuesday and Friday". If the
# health check flags one of them, port it here the same way.
