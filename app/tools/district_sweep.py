"""District checker: one real address in every postcode district of every council we cover,
looked up the way a sign-up would, to find areas that fail before a resident does.

    docker exec -d binny-api-1 python -m app.tools.district_sweep    # ~1.5-2 hours, background
    (the address list is throttled to ~1 a second so real sign-ups keep theirs: ~70 min minimum)
    docker exec binny-api-1 cat /tmp/district_sweep.progress          # how far it's got
    docker exec binny-api-1 cat /tmp/district_sweep.txt               # the summary when done

1. Finds every postcode district (GL8, SW1A...) by asking postcodes.io which ones exist, and a
   random postcode in each, sampled a few times so a district split between councils is
   tested in each of them. Cached in /tmp/district_targets.json, so a rerun skips this.
2. For each (district, council): the free address list for that postcode (never the paid
   fallback), a home with a UPRN, then the council's scrapers in order, through the scraper
   container, exactly like a sign-up. Nothing is written to the database.
3. Writes /tmp/district_sweep.csv (one row per district) and /tmp/district_sweep.txt, and posts
   the summary to SLACK_ALERTS_CHANNEL.

Polite: a handful of lookups at a time; councils that need a browser queue for the scraper
container's browser sessions like any sign-up.
"""
import concurrent.futures
import csv
import json
import random
import string
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

from app.adapters.base import AdapterError, LookupInput, NoCollectionsForAddress, unreachable
from app.adapters.registry import candidates
from app.adapters.ukbcd import UKBCDAdapter
from app.core.config import get_settings
from app.services import geo

AREAS = ("AB AL B BA BB BD BH BL BN BR BS BT CA CB CF CH CM CO CR CT CV CW DA DD DE DG DH DL "
         "DN DT DY E EC EH EN EX FK FY G GL GU HA HD HG HP HR HS HU HX IG IP IV KA KT KW KY L "
         "LA LD LE LL LN LS LU M ME MK ML N NE NG NN NP NR NW OL OX PA PE PH PL PO PR RG RH RM "
         "S SA SE SG SK SL SM SN SO SP SR SS ST SW SY TA TD TF TN TQ TR TS TW UB W WA WC WD WF "
         "WN WR WS WV YO ZE").split()
# Central London districts with a letter (EC1A, W1D, SW1A...)
LETTERED = [("EC", 1, 4), ("WC", 1, 2), ("W", 1, 1), ("SW", 1, 1), ("SE", 1, 1), ("E", 1, 1),
            ("N", 1, 1), ("NW", 1, 1)]
SAMPLES = 3  # random postcodes per district: finds the councils a district straddles
WORKERS = 8
# The address service (Ordnance Survey) is shared with real sign-ups and rate-limits bursts:
# one lookup every ADDRESS_GAP seconds leaves them plenty of room.
ADDRESS_GAP = 1.2
_address_lock = threading.Lock()
_last_address = [0.0]
OUT = Path("/tmp")


def _get(http: httpx.Client, url: str, **params):
    for attempt in range(3):
        try:
            r = http.get(url, params=params or None)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            r.raise_for_status()
            return r.json().get("result")
        except httpx.HTTPError:
            time.sleep(1 + attempt)
    return None


def candidate_outcodes() -> list[str]:
    out = [f"{a}{n}" for a in AREAS for n in range(0, 100)]
    out += [f"{a}{n}{c}" for a, lo, hi in LETTERED for n in range(lo, hi + 1)
            for c in string.ascii_uppercase]
    return out


def find_targets() -> list[dict]:
    """[{outcode, gss, council, postcode}] for every district and council in it."""
    cache = OUT / "district_targets.json"
    if cache.exists():
        return json.loads(cache.read_text())
    base = get_settings().postcodes_io_url.rstrip("/")
    http = httpx.Client(timeout=15)

    def probe(oc: str) -> list[dict]:
        if _get(http, f"{base}/outcodes/{oc}") is None:  # no such district
            return []
        found: dict[str, dict] = {}
        for _ in range(SAMPLES):
            res = _get(http, f"{base}/random/postcodes", outcode=oc)
            if res and (res.get("codes") or {}).get("admin_district"):
                gss = res["codes"]["admin_district"]
                found.setdefault(gss, {"outcode": oc, "gss": gss,
                                       "council": res.get("admin_district") or gss,
                                       "postcode": res["postcode"]})
        return list(found.values())

    targets: list[dict] = []
    ocs = candidate_outcodes()
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for i, rows in enumerate(ex.map(probe, ocs), 1):
            targets += rows
            if i % 200 == 0:
                _progress(f"finding districts: {i}/{len(ocs)} checked, {len(targets)} targets")
    cache.write_text(json.dumps(targets))
    return targets


def _addresses(pc: str) -> list:
    """The free address list, throttled, waiting out the service's rate limit (429) rather
    than recording a false 'no address list'."""
    s = get_settings()
    for attempt in range(4):
        with _address_lock:
            wait = _last_address[0] + ADDRESS_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_address[0] = time.monotonic()
        errors: list[str] = []
        hits = geo._fetch_addresses(s, geo.normalise_postcode(pc), paid_ok=False,
                                    errors=errors, alert=False)
        if hits or not any("429" in e for e in errors):
            if errors and not hits:
                raise RuntimeError(errors[-1])
            return hits
        time.sleep(15 * (attempt + 1))  # rate-limited: back off, then try again
    raise RuntimeError("address service kept rate-limiting (429)")


def _pick_addresses(pc: str) -> list:
    hits = [a for a in _addresses(pc) if a.uprn]
    if not hits:
        return []
    mid = len(hits) // 2
    return [hits[mid]] + ([hits[0]] if len(hits) > 1 and hits[0] is not hits[mid] else [])


def check(t: dict) -> dict:
    row = {**t, "result": "", "scraper": "", "secs": "", "error": ""}
    specs = candidates(t["gss"], t["council"])
    if not specs:
        row["result"] = "no scraper"
        return row
    started = time.monotonic()
    try:
        homes = _pick_addresses(t["postcode"])
    except Exception as e:  # noqa: BLE001
        homes, row["error"] = [], f"address lookup: {e}"[:200]
    if not homes:
        row["result"] = "no address list"
        row["secs"] = round(time.monotonic() - started, 1)
        return row
    errors: list[str] = []
    for home in homes:  # a second home when the first has nothing listed (flats, shops)
        lookup = LookupInput(postcode=t["postcode"], uprn=home.uprn,
                             house_number=home.house_number, address_line=home.line)
        no_collections = False
        for spec in specs:
            try:
                items = UKBCDAdapter(spec).fetch(lookup)
                row.update(result=f"ok ({len(items)} dates)", scraper=spec.module, error="")
                row["secs"] = round(time.monotonic() - started, 1)
                return row
            except NoCollectionsForAddress as e:
                no_collections = True
                errors.append(f"{spec.module}: {e}")
            except AdapterError as e:
                errors.append(f"{spec.module}: {e}")
                if unreachable(str(e)):
                    break
            except Exception as e:  # noqa: BLE001 - a sweep never stops for one district
                errors.append(f"{spec.module}: {type(e).__name__}: {e}")
        if not no_collections:
            break
    last = errors[-1] if errors else ""
    row["result"] = ("no collections listed" if "no collections" in last.lower()
                     else "blocked/down" if unreachable(last) else "failed")
    row["error"] = last[:200]
    row["scraper"] = specs[0].module
    row["secs"] = round(time.monotonic() - started, 1)
    return row


def _write_csv(rows: list[dict]) -> None:
    with open(OUT / "district_sweep.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["council", "gss", "outcode", "postcode", "result",
                                           "scraper", "secs", "error"])
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["council"], r["outcode"])):
            w.writerow({k: r.get(k, "") for k in w.fieldnames})


def _progress(text: str) -> None:
    (OUT / "district_sweep.progress").write_text(text + "\n")


def summarise(rows: list[dict]) -> str:
    by = Counter(r["result"].split(" (")[0] for r in rows)
    ok = by.get("ok", 0)
    lines = [f"*District check*: {len(rows)} districts × councils, {ok} ok "
             f"({round(100 * ok / max(1, len(rows)))}%)",
             "  ·  ".join(f"{k}: {v}" for k, v in by.most_common() if k != "ok")]
    bad = defaultdict(list)
    for r in rows:
        if not r["result"].startswith("ok"):
            bad[r["council"]].append(r)
    totals = Counter(r["council"] for r in rows)
    worst = sorted(bad.items(), key=lambda kv: (-len(kv[1]) / totals[kv[0]], -len(kv[1])))
    if worst:
        lines.append("")
        lines.append("*Councils with failures* (failed / districts checked, example):")
    for council, rs in worst[:25]:
        eg = rs[0]
        lines.append(f"• *{council}*: {len(rs)}/{totals[council]} · {eg['outcode']} "
                     f"{eg['result']}" + (f" · `{eg['error'][:110]}`" if eg["error"] else ""))
    if len(worst) > 25:
        lines.append(f"…and {len(worst) - 25} more councils (full list: /tmp/district_sweep.csv)")
    return "\n".join(lines)


def main(limit: int | None = None) -> None:
    t0 = time.monotonic()
    targets = find_targets()
    random.Random(1).shuffle(targets)  # spread councils out, so no site gets a burst
    if limit:
        targets = targets[:limit]
    rows: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(check, t) for t in targets]
        for f in concurrent.futures.as_completed(futs):
            try:
                rows.append(f.result())
            except Exception as e:  # noqa: BLE001
                rows.append({"outcode": "?", "gss": "?", "council": "?", "postcode": "?",
                             "result": "failed", "scraper": "", "secs": "", "error": str(e)[:200]})
            if len(rows) % 10 == 0:
                _progress(f"checked {len(rows)}/{len(targets)} "
                          f"({round((time.monotonic() - t0) / 60)} min)")
            if len(rows) % 100 == 0:
                _write_csv(rows)  # partial results, in case it's stopped
    rows.sort(key=lambda r: (r["council"], r["outcode"]))
    _write_csv(rows)
    text = summarise(rows) + f"\n_({round((time.monotonic() - t0) / 60)} min)_"
    (OUT / "district_sweep.txt").write_text(text)
    _progress(f"done: {len(rows)} checked")
    s = get_settings()
    if s.slack_bot_token and s.slack_alerts_channel:
        from app.services import inbox

        inbox._api("chat.postMessage", channel=s.slack_alerts_channel, text=text,
                   unfurl_links=False)
    print(text)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
