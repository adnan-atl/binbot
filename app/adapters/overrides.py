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


# ---- Kensington and Chelsea (no library module) ------------------------------------
# rbkc.gov.uk lists collection days by street, one row per stretch of a long street:
#   Street | Property numbers | Number details    | Further details | Waste+recycling | Food
#   Cromwell Road | [2 - 100] | Even numbers only | North Side ...  | Monday, Thursday |
#   Portobello Road | [2 - 80a evens] & [9 - 63 odds] | Both sides | ... | Monday, Thursday | Monday
# General waste and recycling go out on the same days. The street comes from the address
# (the resident picked it at sign-up); the house number picks the row.

RBKC_URL = "https://www.rbkc.gov.uk/bincollections/default.aspx"
_RANGE = re.compile(r"\[\s*(\d+)\s*[a-z]?\s*-\s*(\d+)\s*[a-z]?\s*(odds?|evens?)?\s*\]", re.I)


def _norm_street(s: str) -> str:
    return re.sub(r"[^A-Z0-9 ]", "", re.sub(r"\s+", " ", s.upper())).strip()


def rbkc_street_in(address: str, streets: list[str]) -> str | None:
    """The council's street name that appears in an address line (longest wins, so
    'Kensington High Street' beats 'High Street')."""
    a = f" {_norm_street(address)} "
    hits = [st for st in streets if f" {_norm_street(st)} " in a]
    return max(hits, key=len) if hits else None


def rbkc_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tbody tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 5:
            continue
        rows.append({"numbers": cells[1], "parity": cells[2].lower(), "details": cells[3],
                     "waste": _split_days(cells[4]) if cells[4] else [],
                     "food": _split_days(cells[5]) if len(cells) > 5 and cells[5] else []})
    return rows


def _row_covers(row: dict, n: int) -> tuple[bool, int]:
    """(covers house n, size of the stretch: smaller = more specific)."""
    side = "odd" if "odd" in row["parity"] else "even" if "even" in row["parity"] else None
    if row["numbers"].strip().lower() == "all":
        return (side is None or (n % 2 == 1) == (side == "odd")), 10**6
    best = None
    for lo, hi, par in _RANGE.findall(row["numbers"]):
        lo, hi = sorted((int(lo), int(hi)))
        want = ("odd" if par.lower().startswith("odd") else "even") if par else side
        if lo <= n <= hi and (want is None or (n % 2 == 1) == (want == "odd")):
            best = min(best or 10**6, hi - lo)
    return (best is not None), (best if best is not None else 10**6)


def rbkc_pick(rows: list[dict], house: str | None) -> dict:
    if not rows:
        raise NoCollectionsForAddress("rbkc.gov.uk lists no collections for that street")
    same = {(tuple(r["waste"]), tuple(r["food"])) for r in rows}
    n = None
    m = re.findall(r"\d+", house or "")
    if m:
        n = int(m[-1])  # 'Flat 3, 18' -> the building, 18
    if n is not None:
        covering = sorted((size, i) for i, r in enumerate(rows) for ok, size in [_row_covers(r, n)] if ok)
        if covering:
            return rows[covering[0][1]]
    if len(same) == 1:  # the whole street shares one set of days
        return rows[0]
    if n is None:
        raise MissingInput("house_number")
    raise NoCollectionsForAddress(f"rbkc.gov.uk has no row for number {n} on that street")


def rbkc_collections(row: dict, today: date) -> list[RawCollection]:
    out = []
    for day in row["waste"]:
        for d in _upcoming(day, today):
            out += [RawCollection("General waste", d), RawCollection("Recycling", d)]
    for day in row["food"]:
        out += [RawCollection("Food waste", d) for d in _upcoming(day, today)]
    if not out:
        raise NoCollectionsForAddress("rbkc.gov.uk lists no collection days for that address")
    return sorted(out, key=lambda c: (c.date, c.label))


def kensington_and_chelsea(lookup: LookupInput, today: date | None = None) -> list[RawCollection]:
    today = today or date.today()
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as c:
            page = c.get(RBKC_URL)
            page.raise_for_status()
            soup = BeautifulSoup(page.text, "html.parser")
            select = soup.find("select", {"name": "Street"})
            token = soup.find("input", {"name": "__RequestVerificationToken"})
            if select is None or token is None:
                raise AdapterError("rbkc.gov.uk: page layout changed", retryable=False)
            options = {o.get_text(strip=True): o.get("value", "") for o in select.find_all("option")
                       if o.get("value", "").strip()}
            street = _rbkc_street(lookup, list(options))
            r = c.post(RBKC_URL, data={"Street": options[street],
                                       "__RequestVerificationToken": token["value"]})
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise AdapterError(f"rbkc.gov.uk: {e}") from e
    return rbkc_collections(rbkc_pick(rbkc_rows(r.text), lookup.house_number), today)


def _rbkc_street(lookup: LookupInput, streets: list[str]) -> str:
    """From the address the resident picked; failing that, the postcode's address list."""
    if lookup.address_line:
        st = rbkc_street_in(lookup.address_line, streets)
        if st:
            return st
    from app.services import geo

    try:
        hits = geo.lookup_addresses(lookup.postcode)
    except Exception:
        hits = []
    mine = [h for h in hits if (lookup.uprn and h.uprn == lookup.uprn)
            or (lookup.house_number and h.house_number == lookup.house_number)]
    found = {st for h in (mine or hits) for st in [rbkc_street_in(h.line, streets)] if st}
    if len(found) == 1:
        return found.pop()
    if not found:
        raise NoCollectionsForAddress(f"rbkc.gov.uk: no listed street for {lookup.postcode}")
    raise MissingInput("house_number")  # postcode spans streets: need the exact address


OVERRIDES = {
    "LondonBoroughHammersmithandFulham": hammersmith_and_fulham,
    "GlasgowCityCouncil": glasgow,
    "BinBotKensingtonAndChelsea": kensington_and_chelsea,  # registry.CUSTOM
}

# Same weakness upstream, not yet reproduced against a live page: LondonBoroughLewisham,
# NewForestCouncil and ShetlandIslandsCouncil also read a single weekday name per bin.
# Lewisham's regex would silently keep only the first of "Tuesday and Friday". If the
# health check flags one of them, port it here the same way.
