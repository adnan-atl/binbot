"""Local replacements for UKBinCollectionData scrapers that are broken upstream.

When a council's library module breaks (site changed, edge case it never handled), fix it
here instead of waiting for a library release. Key = library module name. Remove the
entry once upstream catches up; the health check will tell you if the library version
starts working again.
"""
import json
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


# ---- Newham ---------------------------------------------------------------------------
# Upstream (NewhamCouncil) reads "Tuesday 10/11/2026" month-first, so 10 Nov becomes 11 Oct
# (any day up to the 12th comes out wrong). The page shows each bin's next and previous
# dates, UK order, with the weekday: read them day-first, check the weekday, and use the gap
# between previous and next for the frequency.

NEWHAM_BINS = {"domestic": "Domestic waste", "recycling": "Recycling", "food waste": "Food waste"}
NEWHAM_UA = "Binjamin/1.0 (+https://binjamin.uk)"
_NEWHAM_DATE = re.compile(r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\W+(\d{1,2})/(\d{1,2})/(\d{4})", re.I)


def _newham_date(m: re.Match) -> date | None:
    wd, a, b, y = WEEKDAYS[m.group(1).lower()], int(m.group(2)), int(m.group(3)), int(m.group(4))
    for d, mo in ((a, b), (b, a)):  # day-first, as printed; month-first only if that's the one that fits
        try:
            dt = date(y, mo, d)
        except ValueError:
            continue
        if dt.weekday() == wd:
            return dt
    return None


def newham(lookup: LookupInput, *, html: str | None = None,
           today: date | None = None) -> list[RawCollection]:
    today = today or date.today()
    if html is None:
        if not lookup.uprn:
            raise MissingInput("uprn")
        url = f"https://bincollection.newham.gov.uk/Details/Index/{lookup.uprn}"
        try:
            # The council's certificate chain is incomplete; the library skips verification too
            r = httpx.get(url, headers={"User-Agent": NEWHAM_UA}, timeout=30, verify=False)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise AdapterError(f"newham.gov.uk: {e}") from e
        html = r.text
    soup = BeautifulSoup(html, "html.parser")
    out: list[RawCollection] = []
    for card in soup.find_all("div", class_="card"):
        head = card.find("div", class_="card-header")
        text = card.get_text(" ", strip=True).replace("\xa0", " ")
        label = next((v for k, v in NEWHAM_BINS.items()
                      if head and re.search(rf"\b{k}\b", head.get_text(" ", strip=True), re.I)), None)
        if not label:
            continue
        dates = [d for d in (_newham_date(m) for m in _NEWHAM_DATE.finditer(text)) if d]
        if not dates:
            continue
        nxt = dates[0]
        gap = (nxt - dates[1]).days if len(dates) > 1 else 0
        step = gap if gap in (7, 14, 21, 28) else 0
        series = [nxt + timedelta(days=step * i) for i in range(WEEKS_AHEAD)] if step else [nxt]
        out += [RawCollection(label=label, date=d) for d in series if d >= today]
    if not out:
        raise NoCollectionsForAddress("newham.gov.uk: no collection dates for this address")
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
            r = httpx.get(url, headers={"User-Agent": UA}, timeout=12, follow_redirects=True)  # it answers in 1-2s when it answers
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



# ---- Cotswold (no browser) ---------------------------------------------------------
# The library's CotswoldDistrictCouncil drives a real browser through the council's Salesforce
# community page, with fixed sleeps: ~27s a lookup, and it breaks if the page is slow. This asks
# the same Salesforce site directly, the way the page itself does, in ~2-3s: start the
# "WebFormAlloyWasteCollectionEnquiry" flow as a guest, look the address up, press Next, and
# read the table the flow returns. Tried first (registry.CUSTOM_FIRST); the browser scraper is
# the fallback if the council changes its site.

COTSWOLD = "https://community.cotswold.gov.uk"
COTSWOLD_PAGE = "/s/waste-collection-enquiry"
COTSWOLD_FLOW = "WebFormAlloyWasteCollectionEnquiry"
_APP = "siteforce:communityApp"


def _cotswold_context(http: httpx.Client, base: str = COTSWOLD) -> dict:
    """The page's Aura context (framework id + app version), read from its HTML."""
    html = http.get(base + COTSWOLD_PAGE).text
    fw = re.search(r'fwuid%22%3A%22([^%]+)%22|"fwuid":"([^"]+)"', html)
    app = re.search(r'APPLICATION@markup://siteforce:communityApp(?:%22%3A%22|":")([^%"]+)', html)
    if not fw or not app:
        raise AdapterError("cotswold: couldn't read the page's framework id (site changed?)")
    return {"mode": "PROD", "fwuid": fw.group(1) or fw.group(2), "app": _APP,
            "loaded": {f"APPLICATION@markup://{_APP}": app.group(1)},
            "dn": [], "globals": {}, "uad": False}


def _aura(http: httpx.Client, ctx: dict, descriptor: str, params: dict,
          base: str = COTSWOLD) -> dict:
    import json

    body = {"message": json.dumps({"actions": [{"id": "1;a", "descriptor": descriptor,
                                                "callingDescriptor": "UNKNOWN", "params": params}]}),
            "aura.context": json.dumps(ctx), "aura.pageURI": COTSWOLD_PAGE, "aura.token": "null"}
    r = http.post(base + "/s/sfsites/aura?r=1", data=body)
    r.raise_for_status()
    action = json.loads(r.text.removeprefix("while(1);"))["actions"][0]
    if action.get("state") != "SUCCESS":
        raise AdapterError(f"cotswold: {descriptor.rsplit('$', 1)[-1]} failed: "
                           f"{str(action.get('error'))[:200]}")
    return action.get("returnValue") or {}


def _cotswold_pick(records: list[dict], house: str | None, line: str | None) -> dict | None:
    """The council's record for this home: by house number or name, else the address line."""
    def name(r):
        return ((r.get("fields") or {}).get("Name") or {}).get("value") or ""
    house = (house or "").strip().upper()
    first = (line or "").split(",")[0].strip().upper()
    for r in records:
        n = name(r).upper()
        if first and re.match(rf"^{re.escape(first)}([\s,]|$)", n):
            return r
    if house:  # "26 LONG STREET" before "FLAT 2, 26 LONG STREET"
        for pattern in (rf"^{re.escape(house)}[\s,]", rf",\s*{re.escape(house)}[\s,]"):
            for r in records:
                if re.search(pattern, name(r).upper()):
                    return r
    return None


def _find_value(node, key: str):
    """The value of the flow output/screen field called `key`, anywhere in the response."""
    if isinstance(node, dict):
        if node.get("name") == key and isinstance(node.get("value"), str):
            return node["value"]
        for v in node.values():
            found = _find_value(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_value(v, key)
            if found is not None:
                return found
    return None


def _cotswold_date(text: str, today: date) -> date | None:
    t = text.strip().lower()
    if t == "today":
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    try:
        d = datetime.strptime(re.sub(r"[^\w\s,]", "", text).strip(), "%a, %d %B").date()
    except ValueError:
        return None
    d = d.replace(year=today.year)
    return d.replace(year=today.year + 1) if d < today else d


def cotswold_direct(lookup: LookupInput, *, client: httpx.Client | None = None,
                    today: date | None = None, base: str = COTSWOLD,
                    client_code: str = "CDC") -> list[RawCollection]:
    """Also Forest of Dean: same Salesforce form (Ubico runs both), its own site and client code."""
    import json

    today = today or date.today()
    house = lookup.house_number or (lookup.address_line or "").split(",")[0] or None
    if not house:
        raise MissingInput("house_number")
    http = client or httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    try:
        ctx = _cotswold_context(http, base)
        started = _aura(http, ctx, "aura://FlowRuntimeConnectController/ACTION$startFlow", {
            "flowDevName": COTSWOLD_FLOW, "enableTrace": False, "enableRollbackMode": False,
            "debugAsUserId": "", "useLatestSubflow": False, "isBuilderDebug": False,
            "arguments": json.dumps([{"name": "vClientCode", "type": "String",
                                      "supportsRecordId": False, "value": client_code}])},
                        base)
        state = (started.get("response") or {}).get("serializedEncodedState")
        record = None
        for q in (f"{house} {lookup.postcode}", lookup.postcode):
            found = _aura(http, ctx, "aura://LookupController/ACTION$lookup", {
                "objectApiName": "Case", "fieldApiName": "Property__c", "pageParam": 1,
                "pageSize": 50, "q": q, "searchType": "TypeAhead", "targetApiName": "Property__c",
                "body": {"sourceRecord": {"apiName": "Case", "fields": {"Id": None}}}}, base)
            records = [r for res in (found.get("lookupResults") or {}).values()
                       for r in (res.get("records") or [])]
            record = _cotswold_pick(records, lookup.house_number, lookup.address_line)
            if record:
                break
        if record is None:
            raise NoCollectionsForAddress(f"cotswold: no property matching {house}, {lookup.postcode}")
        rid = record.get("id") or record["fields"]["Id"]["value"]
        rname = record["fields"]["Name"]["value"]
        fields = [{"field": "FlowStages1.currentStage", "value": None, "isVisible": False},
                  {"field": "FlowStages1.stages", "value": None, "isVisible": False},
                  {"field": "Property.recordId", "value": rid, "isVisible": True},
                  {"field": "Property.recordIds", "value": [rid], "isVisible": True},
                  {"field": "Property.recordName", "value": rname, "isVisible": True}]
        action, resp = "NEXT", {}
        for _ in range(6):  # NEXT, then CONTINUE_AFTER_COMMIT until the result screen
            out = _aura(http, ctx, "aura://FlowRuntimeConnectController/ACTION$navigateFlow", {
                "request": {"action": action, "serializedState": state, "fields": fields,
                            "uiElementVisited": True, "enableTrace": False, "lcErrors": {},
                            "isBuilderDebug": False}}, base)
            resp = out.get("response") or {}
            status = resp.get("interviewStatus")
            if status == "Error":
                raise AdapterError("cotswold: the council's form returned an error for this address")
            if status != "NEEDS_COMMIT":
                break
            state, action, fields = resp.get("serializedEncodedState"), "CONTINUE_AFTER_COMMIT", []
        rows = json.loads(_find_value(resp, "dataJson") or "[]")
    except httpx.HTTPError as e:
        raise AdapterError(f"{base.removeprefix('https://')}: {e}") from e
    finally:
        if client is None:
            http.close()
    items = []
    for row in rows:
        label, when = (row.get("col1") or "").strip(), _cotswold_date(row.get("col2") or "", today)
        if label and when:
            items.append(RawCollection(label, when))
    if not items:
        raise NoCollectionsForAddress("cotswold: no collections listed for this address")
    return items


FOREST_OF_DEAN = "https://community.fdean.gov.uk"


def forest_of_dean_direct(lookup: LookupInput, **kw) -> list[RawCollection]:
    """Forest of Dean: Cotswold's form on the council's own Salesforce site (~3s, not ~24s)."""
    return cotswold_direct(lookup, base=FOREST_OF_DEAN, client_code="FOD", **kw)


# ---- South Hams -------------------------------------------------------------------
# Upstream (SouthHamsDistrictCouncil) opens the session with python-requests' own user agent,
# then posts as Chrome, and calls .json() on whatever comes back: when the council's site
# answers with anything else (an error page, an empty body) a resident sees "JSONDecodeError"
# and is sent to add their bins by hand. Same two calls here, as one consistent client, with
# a readable error that says what the site actually sent (a 403/5xx counts as unreachable, so
# the proxy fallback can try).

# South Hams' firewall refuses a server that claims to be Chrome (HTTP 403 on the first page)
# but lets a plainly named client in. Checked from our server, Nov 2026.
SOUTH_HAMS_UA = "Binjamin/1.0 (+https://binjamin.uk)"
SOUTH_HAMS = "https://waste.southhams.gov.uk/mycollections"


def fcc_collections(lookup: LookupInput, page_url: str, post_url: str, site: str, *,
                    client: httpx.Client | None = None) -> list[RawCollection]:
    """FCC Environment's council sites (South Hams, West Devon): open the page for a session
    token, post it with the UPRN, read each service's next collection from the tiles."""
    if not lookup.uprn:
        raise MissingInput("uprn")
    http = client or httpx.Client(headers={"User-Agent": SOUTH_HAMS_UA}, timeout=30,
                                  follow_redirects=True)
    try:
        page = http.get(page_url)
        token = http.cookies.get("fcc_session_cookie")
        if not token:
            m = re.search(r'name="fcc_session_token"[^>]*value="([^"]+)"', page.text)
            token = m.group(1) if m else None
        if not token:
            raise AdapterError(f"{site}: no session token (HTTP {page.status_code})")
        r = http.post(post_url, data={"fcc_session_token": token, "uprn": lookup.uprn},
                      headers={"X-Requested-With": "XMLHttpRequest", "Referer": page_url})
        try:
            result = r.json()
        except ValueError:
            snippet = re.sub(r"\s+", " ", r.text[:120]).strip() or "an empty reply"
            raise AdapterError(f"{site}: HTTP {r.status_code}, not JSON ({snippet})")
    except httpx.HTTPError as e:
        raise AdapterError(f"{site}: {e}") from e
    finally:
        if client is None:
            http.close()
    items = []
    for tile in ((result.get("binCollections") or {}).get("tile") or []):
        soup = BeautifulSoup(tile[0] if isinstance(tile, list) else str(tile), "html.parser")
        for div in soup.find_all("div", class_="collectionDiv"):
            name = div.find("h3")
            details = div.find("div", class_="detWrap")
            if not name or not details:
                continue
            m = re.search(r"next scheduled collection is\s+(?:today,\s*|tomorrow,\s*)?"
                          r"([A-Za-z]+,\s*\d{1,2}\s+[A-Za-z]+\s+\d{4})",
                          details.get_text(" ", strip=True))
            if not m:
                continue
            try:
                when = datetime.strptime(re.sub(r"\s+", " ", m.group(1)).replace(" ,", ","),
                                         "%A, %d %B %Y").date()
            except ValueError:
                continue
            items.append(RawCollection(name.get_text(strip=True), when))
    if not items:
        raise NoCollectionsForAddress(f"{site}: no collections listed for this address")
    return items


def south_hams(lookup: LookupInput, *, client: httpx.Client | None = None) -> list[RawCollection]:
    return fcc_collections(lookup, SOUTH_HAMS, SOUTH_HAMS + "/getcollectiondetails",
                           "southhams.gov.uk", client=client)


# West Devon shares South Hams' waste contractor and its site, at a different address. The
# library drives a browser just to get the session cookie, and times out; no browser is needed.
WEST_DEVON = "https://westdevon.fccenvironment.co.uk"


def west_devon(lookup: LookupInput, *, client: httpx.Client | None = None) -> list[RawCollection]:
    return fcc_collections(lookup, WEST_DEVON, WEST_DEVON + "/ajaxprocessor/getcollectiondetails",
                           "westdevon.fccenvironment.co.uk", client=client)

# ---- iTouchVision portal (Somerset) ---------------------------------------------------------
# Somerset moved its bin-day lookup to iTouchVision's portal in November 2026, so the library's
# browser scraper (it drives the old somerset.gov.uk form) waits for a page that no longer
# exists and times out. The portal's own page asks one endpoint, with the UPRN in an
# AES-encrypted header. The key and IV are the portal's public ones, the same the library's
# Newport and Buckinghamshire scrapers use; each council has its own client and council id.
ITOUCH_URL = "https://iweb.itouchvision.com/portal/itouchvision/kmbd/collectionDay"
ITOUCH_KEY = bytes.fromhex("F57E76482EE3DC3336495DEDEEF3962671B054FE353E815145E29C5689F72FEC")
ITOUCH_IV = bytes.fromhex("2CBF4FC35C69B82362D393A4F0B9971A")
SOMERSET_IDS = {"P_CLIENT_ID": 129, "P_COUNCIL_ID": 34493}


def itouch_crypt(data: bytes, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(ITOUCH_KEY), modes.CBC(ITOUCH_IV))
    if encrypt:
        pad, enc = padding.PKCS7(128).padder(), cipher.encryptor()
        return enc.update(pad.update(data) + pad.finalize()) + enc.finalize()
    dec, unpad = cipher.decryptor(), padding.PKCS7(128).unpadder()
    return unpad.update(dec.update(data) + dec.finalize()) + unpad.finalize()


def itouchvision(lookup: LookupInput, ids: dict, site: str, *, names: dict | None = None,
                 client: httpx.Client | None = None) -> list[RawCollection]:
    """Next collection per bin from an iTouchVision council, by UPRN, without a browser."""
    if not lookup.uprn:
        raise MissingInput("uprn")
    payload = json.dumps({"P_UPRN": str(lookup.uprn), **ids, "P_LANG_CODE": "EN"}).encode()
    http = client or httpx.Client(headers={"User-Agent": UA}, timeout=30)
    try:
        r = http.get(ITOUCH_URL, headers={
            "P_PARAMETER": itouch_crypt(payload, encrypt=True).hex(),
            "Origin": "https://iportal.itouchvision.com",
            "Referer": "https://iportal.itouchvision.com/"})
    except httpx.HTTPError as e:
        raise AdapterError(f"{site}: {e}") from e
    finally:
        if client is None:
            http.close()
    try:
        result = json.loads(itouch_crypt(bytes.fromhex(r.text.strip()), encrypt=False))
    except ValueError:  # not hex, bad padding or not JSON: an error page, not our data
        snippet = re.sub(r"\s+", " ", r.text[:120]).strip() or "an empty reply"
        raise AdapterError(f"{site}: HTTP {r.status_code}, unreadable reply ({snippet})")
    items = []
    for row in result.get("collectionDay") or []:
        name = (row.get("binType") or "").strip()
        name = (names or {}).get(name.upper(), name)
        for key in ("collectionDay", "followingDay"):  # the next date, and the one after
            try:
                when = datetime.strptime(str(row.get(key) or ""), "%d-%m-%Y").date()
            except ValueError:
                continue
            if name:
                items.append(RawCollection(name, when))
    if not items:
        raise NoCollectionsForAddress(f"{site}: no collections listed for this address")
    return items


def somerset_direct(lookup: LookupInput, *, client: httpx.Client | None = None) -> list[RawCollection]:
    return itouchvision(lookup, SOMERSET_IDS, "somerset (iTouchVision)", client=client)


# Torfaen is on the same portal (its library scraper drives a browser there and times out).
# The portal names its bins by colour alone; the council's own guide says the purple-lidded bin
# is general waste and the green bin garden waste.
TORFAEN_IDS = {"P_CLIENT_ID": 80, "P_COUNCIL_ID": 397}
TORFAEN_NAMES = {"GREEN": "Garden waste (green bin)",
                 "PURPLE LIDDED BIN": "General waste (purple lidded bin)",
                 "RECYCLING & FOOD": "Recycling and food waste"}


def torfaen(lookup: LookupInput, *, client: httpx.Client | None = None) -> list[RawCollection]:
    return itouchvision(lookup, TORFAEN_IDS, "torfaen (iTouchVision)", names=TORFAEN_NAMES,
                        client=client)


# ---- Westminster ----------------------------------------------------------------------------
# Westminster publishes collection days per street (USRN), not per home. The library module
# takes its "uprn" argument as the street's USRN, but we pass the home's UPRN, so every lookup
# asked for a street that doesn't exist and got a 404. This finds the street the way
# Kensington's does (the street name in the resident's address, else the postcode's address
# list), takes its USRN from the council's street list and reads the same tables.
WESTMINSTER = "https://transact.westminster.gov.uk/env"
_WM_DAYS = {d[:3]: i for d, i in WEEKDAYS.items()}


def westminster_days(text: str) -> set[int]:
    """'Mon - Fri' / 'Sat, Sun' / 'Tue, Fri' / 'Wed' -> weekday numbers."""
    out: set[int] = set()
    for part in re.split(r"[,&]", (text or "").lower()):
        m = re.match(r"\s*([a-z]{3})[a-z]*\s*(?:[-\u2013]\s*([a-z]{3})[a-z]*)?\s*$", part)
        if not m or m.group(1) not in _WM_DAYS:
            continue
        a = _WM_DAYS[m.group(1)]
        b = _WM_DAYS.get(m.group(2) or "", a)
        out.update(range(a, b + 1) if a <= b else [*range(a, 7), *range(0, b + 1)])
    return out


def westminster_services(html: str) -> dict[str, set[int]]:
    """{service: weekdays} from a street report: rubbish (table 1) and recycling (table 2)."""
    tables = BeautifulSoup(html, "html.parser").find_all("table")
    services: dict[str, set[int]] = {}
    for i, table in enumerate(tables[:2]):
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if i == 0 and len(cells) >= 4:
                name, days = "Rubbish Collection", westminster_days(cells[1]) | westminster_days(cells[3])
            elif i == 1 and len(cells) >= 6:
                if "business" in cells[1].lower():
                    continue  # commercial blue bags, not residents
                name = cells[1] or "Recycling Collection"
                days = westminster_days(cells[3]) | westminster_days(cells[5])
            else:
                continue
            if days:
                services.setdefault(name, set()).update(days)
    return services


def westminster(lookup: LookupInput, *, client: httpx.Client | None = None,
                today: date | None = None) -> list[RawCollection]:
    today = today or date.today()
    http = client or httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    try:
        page = http.get(f"{WESTMINSTER}/streetsearch.aspx")
        page.raise_for_status()
        select = BeautifulSoup(page.text, "html.parser").find("select", {"id": "dlstreets"})
        if select is None:
            raise AdapterError("westminster.gov.uk: street list not found (page changed)",
                               retryable=False)
        usrns: dict[str, str] = {}
        for o in select.find_all("option"):
            if o.get("value", "").strip() and o.get_text(strip=True):
                usrns.setdefault(o.get_text(strip=True), o["value"].strip())
        street = _rbkc_street(lookup, list(usrns))
        r = http.get(f"{WESTMINSTER}/streetreport.aspx", params={"USRN": usrns[street]})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise AdapterError(f"westminster.gov.uk: {e}") from e
    finally:
        if client is None:
            http.close()
    out = [RawCollection(name, d) for name, days in westminster_services(r.text).items()
           for n in range(1, 7 * WEEKS_AHEAD + 1)
           for d in [today + timedelta(days=n)] if d.weekday() in days]
    if not out:
        raise NoCollectionsForAddress(f"westminster.gov.uk lists no residential collections on {street}")
    return sorted(out, key=lambda c: (c.date, c.label))


# ---- Castle Point -----------------------------------------------------------------------------
# Like Westminster, Castle Point's calendar is per street: the library module's "uprn" is the
# council's 4-digit roadID, and our home UPRN asked for a street that doesn't exist (its page
# then has no calendar and the library fails on the heading). Find the street from the address
# with the council's own street search, then read its calendar. The key on the page: pink days
# are recycling (blue and pink sacks, glass), normal days are refuse (black sacks); food and
# garden waste go out every week.
CASTLE_POINT = "https://apps.castlepoint.gov.uk/cpapps/index.cfm"
CASTLE_POINT_DAYS = {
    "pink": ["Recycling (pink sacks)", "Paper and card (blue sacks)", "Glass", "Food waste",
             "Garden waste"],
    "normal": ["Refuse (black sacks)", "Food waste", "Garden waste"],
}
_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)


def address_lines(lookup: LookupInput) -> list[str]:
    """The resident's own address line; failing that, this home's line(s) in the postcode's
    address list (by UPRN or house number), else every line in the postcode."""
    if lookup.address_line:
        return [lookup.address_line]
    from app.services import geo

    try:
        hits = geo.lookup_addresses(lookup.postcode)
    except Exception:
        hits = []
    mine = [h.line for h in hits if (lookup.uprn and h.uprn == lookup.uprn)
            or (lookup.house_number and h.house_number == lookup.house_number)]
    return mine or [h.line for h in hits]


def street_parts(line: str) -> list[str]:
    """Comma parts of an address line that could be a street: not the number, not the postcode."""
    parts = []
    for part in line.split(","):
        p = _POSTCODE.sub("", part).strip()
        p = re.sub(r"^(flat|apartment|unit)\s+\S+\s*", "", p, flags=re.I)
        p = re.sub(r"^\d+[a-z]?(\s*-\s*\d+[a-z]?)?\s+", "", p, flags=re.I).strip()
        if re.search(r"[A-Za-z]{3}", p):
            parts.append(p)
    return parts


def castle_point_calendar(html: str) -> list[RawCollection]:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one("div.contentContainer h1")
    m = re.search(r"([A-Za-z]+)\s+(\d{4})", h1.get_text(" ", strip=True) if h1 else "")
    if not m:
        raise NoCollectionsForAddress("castlepoint.gov.uk: no calendar for that street")
    year, prev = int(m.group(2)), datetime.strptime(m.group(1), "%B").month
    out = []
    for box in soup.select("div.calendarContainer div.calendarContainer"):
        head = box.find("tr", class_="calendar")
        try:
            month = datetime.strptime(head.get_text(strip=True), "%B").month
        except (AttributeError, ValueError):
            continue
        if month < prev:
            year += 1  # December -> January
        prev = month
        for kind, labels in CASTLE_POINT_DAYS.items():
            for td in box.find_all("td", class_=kind):
                if td.get_text(strip=True).isdigit():
                    d = date(year, month, int(td.get_text(strip=True)))
                    out += [RawCollection(label, d) for label in labels]
    if not out:
        raise NoCollectionsForAddress("castlepoint.gov.uk lists no collection days for that street")
    return sorted(out, key=lambda c: (c.date, c.label))


def castle_point(lookup: LookupInput, *, client: httpx.Client | None = None) -> list[RawCollection]:
    lines = address_lines(lookup)
    if not lines:
        raise NoCollectionsForAddress(f"castlepoint.gov.uk: no address found for {lookup.postcode}")
    http = client or httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    try:
        road = None
        for part in dict.fromkeys(p for line in lines for p in street_parts(line)):
            r = http.post(CASTLE_POINT, params={"fa": "mystreet.search"}, data={"searchterm": part})
            r.raise_for_status()
            found = []
            for a in BeautifulSoup(r.text, "html.parser").find_all("a"):
                name, _, town = a.get_text(" ", strip=True).partition(" - ")
                rid = re.search(r"roadID=(\d+)", a.get("href") or "")
                if rid and town and _norm_street(name) == _norm_street(part):
                    found.append((town, rid.group(1)))
            if found:
                town_hit = [rid for town, rid in found
                            if any(_norm_street(town) in _norm_street(line) for line in lines)]
                road = (town_hit or [rid for _, rid in found])[0]
                break
        if road is None:
            raise NoCollectionsForAddress(f"castlepoint.gov.uk: street not found for {lookup.postcode}")
        r = http.post(CASTLE_POINT, params={"fa": "myStreet.displayDetails"}, data={"roadID": road})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise AdapterError(f"castlepoint.gov.uk: {e}") from e
    finally:
        if client is None:
            http.close()
    return castle_point_calendar(r.text)


# ---- East Herts ---------------------------------------------------------------------------
# East Herts' AchieveForms form retired the lookup the library calls (683d9ff0e299d now answers
# "User Package Not Found", so the library fails with KeyError 'integration'). The same form's
# collection lookup is now 6accfd6441228 (read from the form's autoLookup field, Nov 2026); it
# returns each service's name and next date ("Tuesday 17th November", no year).
EAST_HERTS = "https://eastherts-self.achieveservice.com"
EAST_HERTS_LOOKUP = "6accfd6441228"
_EH_PROCESS = "AF-Process-98782935-6101-4962-9a55-5923e76057b6"
_EH_FORM = (f"sandbox-publish://{_EH_PROCESS}/AF-Stage-dcd0ec18-dfb4-496a-a266-bd8fadaa28a7/"
            "definition.json")


def east_herts_rows(rows: dict, today: date) -> list[RawCollection]:
    out = []
    for key, value in rows.items():
        if not key.endswith("NextDate") or not str(value or "").strip():
            continue
        name = re.sub(r"\s+", " ", str(rows.get(key[:-8] + "ServiceName") or key[:-8])).strip()
        text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", str(value)).strip()
        try:
            d = datetime.strptime(f"{text} {today.year}", "%A %d %B %Y").date()
        except ValueError:
            try:  # weekday doesn't match this year's date: it's next year's
                d = datetime.strptime(f"{text.split(' ', 1)[-1]} {today.year}", "%d %B %Y").date()
            except ValueError:
                continue
        if d < today - timedelta(days=7):
            d = d.replace(year=d.year + 1)
        out.append(RawCollection(name, d))
    return sorted(out, key=lambda c: (c.date, c.label))


def east_herts(lookup: LookupInput, *, client: httpx.Client | None = None,
               today: date | None = None) -> list[RawCollection]:
    if not lookup.uprn:
        raise MissingInput("uprn")
    today = today or date.today()
    from urllib.parse import quote

    form = (f"{EAST_HERTS}/AchieveForms/?mode=fill&consentMessage=yes&form_uri={_EH_FORM}"
            f"&process=1&process_uri=sandbox-processes://{_EH_PROCESS}&process_id={_EH_PROCESS}")
    http = client or httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    try:
        auth = http.get(f"{EAST_HERTS}/authapi/isauthenticated",
                        params={"uri": quote(form, safe=""), "hostname": "eastherts-self.achieveservice.com",
                                "withCredentials": "true"})
        auth.raise_for_status()
        sid = auth.json()["auth-session"]
        r = http.post(f"{EAST_HERTS}/apibroker/runLookup",
                      params={"id": EAST_HERTS_LOOKUP, "repeat_against": "", "noRetry": "true",
                              "getOnlyTokens": "undefined", "log_id": "", "app_name": "AF-Renderer::Self",
                              "_": str(int(datetime.now().timestamp() * 1000)), "sid": sid},
                      json={"formValues": {"Collection Days": {"inputUPRN": {"value": str(lookup.uprn)}}}},
                      headers={"X-Requested-With": "XMLHttpRequest",
                               "Referer": f"{EAST_HERTS}/fillform/?iframe_id=fillform-frame-1&db_id="})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError, KeyError) as e:
        raise AdapterError(f"eastherts-self.achieveservice.com: {type(e).__name__}: {e}") from e
    finally:
        if client is None:
            http.close()
    if "integration" not in data:
        raise AdapterError(f"eastherts: lookup {EAST_HERTS_LOOKUP} refused ({data.get('error') or 'no data'})",
                           retryable=False)
    rows = ((data["integration"].get("transformed") or {}).get("rows_data") or {}).get("0") or {}
    items = east_herts_rows(rows if isinstance(rows, dict) else {}, today)
    if not items:
        raise NoCollectionsForAddress("eastherts: no collections listed for this address")
    return items


# ---- Gloucester -----------------------------------------------------------------------------
# The library drives a browser through the council's AchieveForms page (~23s). The form itself
# asks a chain of public lookups: the property's bins by UPRN, each bin's round ("Monday Week
# 1"), and that round's next date. Same lookups here, the three rounds in parallel (~5s).
GLOUCESTER = "https://gloucester-self.achieveservice.com"
GLOUCESTER_PAGE = GLOUCESTER + "/service/Bins___Check_your_bin_day"
GLOUCESTER_UA = "Binjamin/1.0 (+https://binjamin.uk)"
_GL_SECTION = "Your waste collections"
_GL_BINS = "63f72ddc8ca25"
# kind -> (round lookup, next-date lookup, our label)
_GL_KINDS = {"Refuse": ("63f731d2b50d7", "63ca72c70c3b1", "General waste"),
             "Recycling": ("63f89f73018c0", "63cfcf4756b5d", "Recycling"),
             "Food": ("63f8a11714712", "63cfcf8ac7877", "Food waste"),
             "Garden": ("63f8a15776b5d", "63cfcfc1c486c", "Garden waste")}


def gloucester_rows(kind: str, schedule: str, next_iso: str, today: date) -> list[RawCollection]:
    """One bin's next date, projected along its round: "Weekly" every 7 days, "Week 1/2" every 14."""
    try:
        nxt = date.fromisoformat(next_iso)
    except (TypeError, ValueError):
        return []
    s = (schedule or "").lower()
    step = 7 if "weekly" in s else 14 if "week" in s else 0
    dates = [nxt + timedelta(days=step * i) for i in range(WEEKS_AHEAD if step == 7 else 4)] if step else [nxt]
    return [RawCollection(_GL_KINDS[kind][2], d) for d in dates if d >= today]


def gloucester(lookup: LookupInput, *, client: httpx.Client | None = None,
               today: date | None = None) -> list[RawCollection]:
    if not lookup.uprn:
        raise MissingInput("uprn")
    import concurrent.futures

    today = today or date.today()
    http = client or httpx.Client(headers={"User-Agent": GLOUCESTER_UA}, timeout=30, follow_redirects=True)
    try:
        auth = http.get(f"{GLOUCESTER}/authapi/isauthenticated",
                        params={"uri": GLOUCESTER_PAGE, "hostname": "gloucester-self.achieveservice.com",
                                "withCredentials": "true"})
        auth.raise_for_status()
        sid = auth.json()["auth-session"]

        def run(lookup_id: str, values: dict) -> dict:
            r = http.post(f"{GLOUCESTER}/apibroker/runLookup",
                          params={"id": lookup_id, "repeat_against": "", "noRetry": "true",
                                  "getOnlyTokens": "undefined", "log_id": "", "app_name": "AF-Renderer::Self",
                                  "_": str(int(datetime.now().timestamp() * 1000)), "sid": sid},
                          json={"formValues": {_GL_SECTION: {k: {"value": v} for k, v in values.items()}}},
                          headers={"X-Requested-With": "XMLHttpRequest"})
            r.raise_for_status()
            rows = ((r.json().get("integration") or {}).get("transformed") or {}).get("rows_data") or {}
            return (rows.get("0") if isinstance(rows, dict) else None) or {}

        uprn = str(lookup.uprn)
        bins = run(_GL_BINS, {"binUprn": uprn})

        def one(kind: str) -> list[RawCollection]:
            bin_id = bins.get(f"{kind}Id")
            if not bin_id:
                return []
            round_lookup, next_lookup, _ = _GL_KINDS[kind]
            sched = run(round_lookup, {"binUprn": uprn, f"{kind}Id": bin_id})
            workflow = sched.get(f"{kind}1")
            if not workflow:
                return []
            nxt = run(next_lookup, {"binUprn": uprn, f"{kind}1": workflow})
            return gloucester_rows(kind, sched.get(f"{kind}_Schedule1", ""), nxt.get(f"Next{kind}1DateISO"), today)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            items = [c for part in ex.map(one, _GL_KINDS) for c in part]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        raise AdapterError(f"gloucester-self.achieveservice.com: {type(e).__name__}: {e}") from e
    finally:
        if client is None:
            http.close()
    if not items:
        raise NoCollectionsForAddress("gloucester: no collections listed for this address")
    return sorted(items, key=lambda c: (c.date, c.label))


# ---- Torbay ---------------------------------------------------------------------------------
# The library drives a browser through torbay.gov.uk by absolute XPaths (cookie banner, a link
# that opens a new tab, then the form) and times out when any of that moves. The form itself is
# a ServiceBuilder form that takes the address as "U<uprn>": read the form's link from the
# council's page, open it, post it with the UPRN, read the dated list it returns.
TORBAY_PAGE = "https://www.torbay.gov.uk/recycling/bin-collections/"
TORBAY_FORM = "https://selfservice-torbay.servicebuilder.co.uk/renderform/Form"
_TORBAY_ROW = re.compile(r"((?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day \d{1,2} [A-Za-z]+ \d{4})\s+"
                         r"([A-Za-z][A-Za-z ]*?Service)\b")


def torbay_rows(html: str) -> list[RawCollection]:
    text = re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" "))
    out = []
    for when, label in _TORBAY_ROW.findall(text):
        try:
            out.append(RawCollection(label.strip(), datetime.strptime(when, "%A %d %B %Y").date()))
        except ValueError:
            continue
    return out


def torbay(lookup: LookupInput, *, client: httpx.Client | None = None) -> list[RawCollection]:
    if not lookup.uprn:
        raise MissingInput("uprn")
    http = client or httpx.Client(headers={"User-Agent": SOUTH_HAMS_UA}, timeout=30,
                                  follow_redirects=True)
    try:
        page = http.get(TORBAY_PAGE)
        page.raise_for_status()
        link = next((a["href"] for a in BeautifulSoup(page.text, "html.parser").find_all("a", href=True)
                     if "servicebuilder.co.uk/renderform" in a["href"]), None)
        if not link:
            raise AdapterError("torbay.gov.uk: bin collections form link not found (page changed)",
                               retryable=False)
        form = http.get(link)
        form.raise_for_status()
        soup = BeautifulSoup(form.text, "html.parser")

        def field(name: str) -> str:
            el = soup.find("input", {"name": name})
            return (el.get("value") or "") if el else ""

        if not field("__RequestVerificationToken"):
            raise AdapterError("torbay: form has no verification token (form changed)", retryable=False)
        r = http.post(TORBAY_FORM, data={
            "__RequestVerificationToken": field("__RequestVerificationToken"),
            "FormGuid": field("FormGuid"), "ObjectTemplateID": field("ObjectTemplateID"),
            "Trigger": "submit", "CurrentSectionID": field("CurrentSectionID") or "0",
            "TriggerCtl": "", "FF1168": f"U{lookup.uprn}",
            "FF1168lbltxt": "Please select your address", "FF1168-text": lookup.postcode})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise AdapterError(f"torbay: {e}") from e
    finally:
        if client is None:
            http.close()
    items = torbay_rows(r.text)
    if not items:
        raise NoCollectionsForAddress("torbay: no upcoming collections listed for this address")
    return items


# ---- Ipswich --------------------------------------------------------------------------------
# Ipswich looks collections up by street name, and the library takes the street from its
# "house number" argument, so our house number found nothing. The street comes from the address;
# long roads are split into stretches ("Norwich Road: Valley Road to Railway Bridge", "... (odd
# numbers only)"), narrowed by the house number's parity. If stretches still differ, we don't guess.
IPSWICH = "https://app.ipswich.gov.uk/bin-collection/"


def ipswich_calendar(html: str) -> list[RawCollection]:
    out = []
    grid = BeautifulSoup(html, "html.parser").find("dl", class_="ibc-calendar-grid")
    for div in (grid.find_all("div", recursive=False) if grid else []):
        dt = div.find("dt", class_="ibc-calendar-entry")
        dd = div.find("dd", class_="ibc-calendar-entry__details")
        if not (dt and dd):
            continue
        day = dt.find("div", class_="ibc-calendar-entry__date")
        month = dt.find("div", class_="ibc-calendar-entry__month")
        num = re.match(r"\s*(\d{1,2})", day.get_text(" ") if day else "")
        try:
            when = datetime.strptime(f"{num.group(1)} {month.get_text(strip=True)}", "%d %B %Y").date()
        except (AttributeError, ValueError):
            continue
        out += [RawCollection(li.get_text(" ", strip=True), when) for li in dd.find_all("li")
                if li.get_text(strip=True)]
    return out


def _ipswich_stretches(html: str, street: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    return [(a.get_text(" ", strip=True), a["href"]) for a in soup.find_all("a", href=True)
            if "/bin-collection/weeks/" in a["href"]
            and _norm_street(a.get_text(" ", strip=True).split(":")[0]) == _norm_street(street)]


def ipswich(lookup: LookupInput, *, client: httpx.Client | None = None) -> list[RawCollection]:
    lines = address_lines(lookup)
    n = re.findall(r"\d+", lookup.house_number or "")
    http = client or httpx.Client(headers={"User-Agent": SOUTH_HAMS_UA}, timeout=30,
                                  follow_redirects=True)
    try:
        for street in dict.fromkeys(p for line in lines for p in street_parts(line)):
            r = http.post(IPSWICH, data={"street-name": street, "submit-button": ""})
            r.raise_for_status()
            items = ipswich_calendar(r.text)
            if items:
                return items
            stretches = _ipswich_stretches(r.text, street)
            if not stretches:
                continue
            if n:
                side = "odd" if int(n[-1]) % 2 else "even"
                fits = [s for s in stretches if f"{'even' if side == 'odd' else 'odd'} numbers only"
                        not in s[0].lower()]
                stretches = fits or stretches
            calendars = []
            for _, href in stretches[:8]:
                page = http.get(httpx.URL(IPSWICH).join(href))
                page.raise_for_status()
                calendars.append(ipswich_calendar(page.text))
            distinct = {tuple((c.label, c.date) for c in cal) for cal in calendars if cal}
            if len(distinct) == 1:
                return next(cal for cal in calendars if cal)
            raise AdapterError(f"ipswich: {street} has {len(stretches)} stretches with different days",
                               retryable=False)
    except httpx.HTTPError as e:
        raise AdapterError(f"ipswich: {e}") from e
    finally:
        if client is None:
            http.close()
    raise NoCollectionsForAddress(f"ipswich: street not found for {lookup.postcode}")


# ---- Chelmsford -----------------------------------------------------------------------------
# The council's address search still names each home's round ("Tuesday B") and links its
# calendar page, but that page no longer offers an .ics file (the library fails with "Could not
# find collection ICS file"): the dates are now listed on the page, a month at a time
# ("Tuesday 3 November: food waste, brown bin, paper sack, plastic and cartons bag").
CHELMSFORD = "https://www.chelmsford.gov.uk/bins-and-recycling/check-your-kerbside-collection-day/"
CHELMSFORD_NAMES = {"green box": "Green box (glass and cans)"}
_CHELMSFORD_DAY = re.compile(r"^\s*[A-Za-z]+day\s+(\d{1,2})\s+([A-Za-z]+)\s*:\s*(.+)$")


def chelmsford_round(html: str, house: str | None, line: str | None) -> str | None:
    """The calendar link for this home from the search results: its own row, else the one
    link every listed home shares."""
    rows = []
    table = BeautifulSoup(html, "html.parser").find("table")
    for tr in (table.find_all("tr") if table else []):
        cells = tr.find_all("td")
        a = tr.find("a", href=lambda h: h and "collection-calendar" in h)
        if len(cells) >= 2 and a:
            rows.append((cells[0].get_text(" ", strip=True), a["href"]))
    parts = [p.strip() for p in (line or "").split(",")]
    want = _norm_street(" ".join(parts[:2])) if len(parts) > 1 else ""
    firsts = [(_norm_street(address.split(",")[0]), href) for address, href in rows]
    for hits in ([h for f, h in firsts if want and f == want],
                 [h for f, h in firsts if house and f.split(" ")[0] == _norm_street(house)],
                 [h for _, h in firsts]):
        if len(set(hits)) == 1:
            return hits[0]
    return None


def chelmsford_calendar(html: str, today: date) -> list[RawCollection]:
    soup = BeautifulSoup(html, "html.parser")
    out, year = [], today.year
    for el in soup.find_all(["h2", "h3", "li"]):
        text = el.get_text(" ", strip=True)
        if el.name != "li":
            m = re.fullmatch(r"[A-Za-z]+\s+(\d{4})", text)
            if m:
                year = int(m.group(1))
            continue
        m = _CHELMSFORD_DAY.match(text)
        if not m:
            continue
        try:
            when = datetime.strptime(f"{m.group(1)} {m.group(2)} {year}", "%d %B %Y").date()
        except ValueError:
            continue
        for item in m.group(3).split(","):
            name = item.strip().rstrip(".")
            if name:
                out.append(RawCollection(CHELMSFORD_NAMES.get(name.lower(), name[:1].upper() + name[1:]), when))
    return out


def chelmsford(lookup: LookupInput, *, client: httpx.Client | None = None,
               today: date | None = None) -> list[RawCollection]:
    today = today or date.today()
    http = client or httpx.Client(headers={"User-Agent": SOUTH_HAMS_UA}, timeout=30,
                                  follow_redirects=True)
    try:
        page = http.get(CHELMSFORD)
        page.raise_for_status()
        box = BeautifulSoup(page.text, "html.parser").find("input", id=re.compile(r"^\d+_keyword$"))
        if box is None:
            raise AdapterError("chelmsford.gov.uk: postcode search not found (page changed)",
                               retryable=False)
        found = http.get(CHELMSFORD, params={box["id"]: lookup.postcode})
        found.raise_for_status()
        href = chelmsford_round(found.text, lookup.house_number, lookup.address_line)
        if not href:
            raise NoCollectionsForAddress(f"chelmsford.gov.uk: no collection round found for "
                                          f"{lookup.house_number or ''} {lookup.postcode}".strip())
        cal = http.get(httpx.URL(CHELMSFORD).join(href))
        cal.raise_for_status()
    except httpx.HTTPError as e:
        raise AdapterError(f"chelmsford.gov.uk: {e}") from e
    finally:
        if client is None:
            http.close()
    items = [c for c in chelmsford_calendar(cal.text, today) if c.date >= today]
    if not items:
        raise NoCollectionsForAddress("chelmsford.gov.uk: the round's calendar lists no upcoming dates")
    return items


# ---- Library modules that want the street where we pass the house number ---------------------
# Their scraper is fine; only its input differs. Rewrite the lookup before the module runs.


def street_in_house_number(lookup: LookupInput) -> LookupInput:
    """Wyre Forest and similar: the street name (from the address) in the house-number field."""
    import dataclasses

    for line in address_lines(lookup):
        parts = street_parts(line)
        if parts:
            return dataclasses.replace(lookup, house_number=parts[0])
    return lookup


INPUT_REWRITES = {
    "WyreForestDistrictCouncil": street_in_house_number,
}


OVERRIDES = {
    "LondonBoroughHammersmithandFulham": hammersmith_and_fulham,
    "NewhamCouncil": newham,
    "GlasgowCityCouncil": glasgow,
    "BinBotKensingtonAndChelsea": kensington_and_chelsea,  # registry.CUSTOM
    "SouthHamsDistrictCouncil": south_hams,
    "WestDevonBoroughCouncil": west_devon,
    "TorfaenCouncil": torfaen,
    "WestminsterCityCouncil": westminster,
    "CastlepointDistrictCouncil": castle_point,
    "EastHertsCouncil": east_herts,
    "TorbayCouncil": torbay,
    "IpswichBoroughCouncil": ipswich,
    "ChelmsfordCityCouncil": chelmsford,
    "BinBotCotswoldDirect": cotswold_direct,  # registry.CUSTOM_FIRST: before the browser one
    "BinBotForestOfDeanDirect": forest_of_dean_direct,  # registry.CUSTOM_FIRST, same form
    "BinBotGloucesterDirect": gloucester,  # registry.CUSTOM_FIRST: before the browser one
    "BinBotSomersetDirect": somerset_direct,  # registry.CUSTOM_FIRST: before the browser one
}

# Same weakness upstream, not yet reproduced against a live page: LondonBoroughLewisham,
# NewForestCouncil and ShetlandIslandsCouncil also read a single weekday name per bin.
# Lewisham's regex would silently keep only the first of "Tuesday and Friday". If the
# health check flags one of them, port it here the same way.
