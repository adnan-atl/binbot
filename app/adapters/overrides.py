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


def _cotswold_context(http: httpx.Client) -> dict:
    """The page's Aura context (framework id + app version), read from its HTML."""
    html = http.get(COTSWOLD + COTSWOLD_PAGE).text
    fw = re.search(r'fwuid%22%3A%22([^%]+)%22|"fwuid":"([^"]+)"', html)
    app = re.search(r'APPLICATION@markup://siteforce:communityApp(?:%22%3A%22|":")([^%"]+)', html)
    if not fw or not app:
        raise AdapterError("cotswold: couldn't read the page's framework id (site changed?)")
    return {"mode": "PROD", "fwuid": fw.group(1) or fw.group(2), "app": _APP,
            "loaded": {f"APPLICATION@markup://{_APP}": app.group(1)},
            "dn": [], "globals": {}, "uad": False}


def _aura(http: httpx.Client, ctx: dict, descriptor: str, params: dict) -> dict:
    import json

    body = {"message": json.dumps({"actions": [{"id": "1;a", "descriptor": descriptor,
                                                "callingDescriptor": "UNKNOWN", "params": params}]}),
            "aura.context": json.dumps(ctx), "aura.pageURI": COTSWOLD_PAGE, "aura.token": "null"}
    r = http.post(COTSWOLD + "/s/sfsites/aura?r=1", data=body)
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
                    today: date | None = None) -> list[RawCollection]:
    import json

    today = today or date.today()
    house = lookup.house_number or (lookup.address_line or "").split(",")[0] or None
    if not house:
        raise MissingInput("house_number")
    http = client or httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    try:
        ctx = _cotswold_context(http)
        started = _aura(http, ctx, "aura://FlowRuntimeConnectController/ACTION$startFlow", {
            "flowDevName": COTSWOLD_FLOW, "enableTrace": False, "enableRollbackMode": False,
            "debugAsUserId": "", "useLatestSubflow": False, "isBuilderDebug": False,
            "arguments": json.dumps([{"name": "vClientCode", "type": "String",
                                      "supportsRecordId": False, "value": "CDC"}])})
        state = (started.get("response") or {}).get("serializedEncodedState")
        record = None
        for q in (f"{house} {lookup.postcode}", lookup.postcode):
            found = _aura(http, ctx, "aura://LookupController/ACTION$lookup", {
                "objectApiName": "Case", "fieldApiName": "Property__c", "pageParam": 1,
                "pageSize": 50, "q": q, "searchType": "TypeAhead", "targetApiName": "Property__c",
                "body": {"sourceRecord": {"apiName": "Case", "fields": {"Id": None}}}})
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
                            "isBuilderDebug": False}})
            resp = out.get("response") or {}
            status = resp.get("interviewStatus")
            if status == "Error":
                raise AdapterError("cotswold: the council's form returned an error for this address")
            if status != "NEEDS_COMMIT":
                break
            state, action, fields = resp.get("serializedEncodedState"), "CONTINUE_AFTER_COMMIT", []
        rows = json.loads(_find_value(resp, "dataJson") or "[]")
    except httpx.HTTPError as e:
        raise AdapterError(f"community.cotswold.gov.uk: {e}") from e
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


def south_hams(lookup: LookupInput, *, client: httpx.Client | None = None) -> list[RawCollection]:
    if not lookup.uprn:
        raise MissingInput("uprn")
    http = client or httpx.Client(headers={"User-Agent": SOUTH_HAMS_UA}, timeout=30,
                                  follow_redirects=True)
    try:
        page = http.get(SOUTH_HAMS)
        token = http.cookies.get("fcc_session_cookie")
        if not token:
            m = re.search(r'name="fcc_session_token"[^>]*value="([^"]+)"', page.text)
            token = m.group(1) if m else None
        if not token:
            raise AdapterError(f"southhams.gov.uk: no session token (HTTP {page.status_code})")
        r = http.post(SOUTH_HAMS + "/getcollectiondetails",
                      data={"fcc_session_token": token, "uprn": lookup.uprn},
                      headers={"X-Requested-With": "XMLHttpRequest", "Referer": SOUTH_HAMS})
        try:
            result = r.json()
        except ValueError:
            snippet = re.sub(r"\s+", " ", r.text[:120]).strip() or "an empty reply"
            raise AdapterError(f"southhams.gov.uk: HTTP {r.status_code}, not JSON ({snippet})")
    except httpx.HTTPError as e:
        raise AdapterError(f"southhams.gov.uk: {e}") from e
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
            m = re.search(r"next scheduled collection is (?:today, |tomorrow, )?([A-Za-z]+, \d{1,2} [A-Za-z]+ \d{4})",
                          details.get_text(" ", strip=True))
            if not m:
                continue
            try:
                when = datetime.strptime(m.group(1), "%A, %d %B %Y").date()
            except ValueError:
                continue
            items.append(RawCollection(name.get_text(strip=True), when))
    if not items:
        raise NoCollectionsForAddress("southhams.gov.uk: no collections listed for this address")
    return items

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


def itouchvision(lookup: LookupInput, ids: dict, site: str, *,
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
        try:
            when = datetime.strptime(str(row.get("collectionDay", "")), "%d-%m-%Y").date()
        except ValueError:
            continue
        if (row.get("binType") or "").strip():
            items.append(RawCollection(row["binType"].strip(), when))
    if not items:
        raise NoCollectionsForAddress(f"{site}: no collections listed for this address")
    return items


def somerset_direct(lookup: LookupInput, *, client: httpx.Client | None = None) -> list[RawCollection]:
    return itouchvision(lookup, SOMERSET_IDS, "somerset (iTouchVision)", client=client)


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


OVERRIDES = {
    "LondonBoroughHammersmithandFulham": hammersmith_and_fulham,
    "GlasgowCityCouncil": glasgow,
    "BinBotKensingtonAndChelsea": kensington_and_chelsea,  # registry.CUSTOM
    "SouthHamsDistrictCouncil": south_hams,
    "WestminsterCityCouncil": westminster,
    "BinBotCotswoldDirect": cotswold_direct,  # registry.CUSTOM_FIRST: before the browser one
    "BinBotSomersetDirect": somerset_direct,  # registry.CUSTOM_FIRST: before the browser one
}

# Same weakness upstream, not yet reproduced against a live page: LondonBoroughLewisham,
# NewForestCouncil and ShetlandIslandsCouncil also read a single weekday name per bin.
# Lewisham's regex would silently keep only the first of "Tuesday and Friday". If the
# health check flags one of them, port it here the same way.
