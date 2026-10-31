"""
Sweep: try every council with the library's own sample address and report what works.

    docker compose exec api python -m app.workers.sweep              # all councils
    docker compose exec api python -m app.workers.sweep Westminster  # name filter
    docker compose exec api python -m app.workers.sweep --no-browser

The samples come from UKBinCollectionData's input.json, so a failure here means the
scraper (or our wiring of it) is broken, not the resident's input. Results are written to
the councils table (status, last_error) so sign-ups and the monitor see them, and printed
grouped by error so the most common breakages can be fixed first.
"""
import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.adapters.base import AdapterError, LookupInput
from app.adapters.registry import MISSING_LAD, _input_json_path, load_specs
from app.adapters.ukbcd import UKBCDAdapter
from app.core.config import get_settings
from app.core.db import session_factory
from app.models import CouncilStatus
from app.services.schedule import _mark_success, get_or_create_council


def _samples() -> dict[str, dict]:
    data = json.loads(_input_json_path().read_text())
    return {m: v for m, v in data.items() if v.get("LAD24CD") or m in MISSING_LAD}


def _try(spec, sample: dict) -> tuple[str, str, int]:
    lookup = LookupInput(
        postcode=sample.get("postcode") or "",
        uprn=str(sample["uprn"]) if sample.get("uprn") else None,
        house_number=str(sample.get("house_number") or sample.get("paon") or sample.get("number") or "")
        or None,
    )
    t = time.monotonic()
    try:
        items = UKBCDAdapter(spec).fetch(lookup)
        return "ok", f"{len(items)} dates, {len({i.label for i in items})} bins", int(time.monotonic() - t)
    except AdapterError as e:
        return "fail", str(e)[:200], int(time.monotonic() - t)
    except Exception as e:  # noqa: BLE001 - report everything
        return "fail", f"{type(e).__name__}: {e}"[:200], int(time.monotonic() - t)


def _kind(err: str) -> str:
    """Bucket errors so the report says what to fix, not 300 different strings."""
    e = err.lower()
    for key, label in (("needs a browser", "no browser configured"),
                       ("selenium", "browser/selenium"), ("webdriver", "browser/selenium"),
                       ("timed out", "timeout"), ("timeout", "timeout"),
                       ("403", "blocked (403)"), ("ssl", "TLS/certificate"),
                       ("no collections", "no dates returned"), ("unparseable date", "date format"),
                       ("nonetype", "page layout changed"), ("index", "page layout changed"),
                       ("keyerror", "page layout changed"), ("json", "API changed")):
        if key in e:
            return label
    return "other"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("filter", nargs="?", default="")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    specs = {s.module: s for s in load_specs().values()}
    samples = _samples()
    todo = [(specs[m], v) for m, v in samples.items() if m in specs
            and args.filter.lower() in (m + specs[m].name).lower()
            and not (args.no_browser and specs[m].needs_browser)]
    if not todo:
        print("No councils match.")
        return 1
    if any(s.needs_browser for s, _ in todo) and not get_settings().selenium_url:
        print("Note: SELENIUM_URL is not set; browser councils will fail.")

    print(f"Sweeping {len(todo)} councils with {args.workers} workers…", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_try, s, v): s for s, v in todo}
        for i, f in enumerate(as_completed(futs), 1):
            s = futs[f]
            status, detail, secs = f.result()
            results.append((s, status, detail, secs))
            print(f"[{i}/{len(todo)}] {status:4} {s.name:40} {secs:3}s  {detail[:90]}", flush=True)

    db = session_factory()()
    try:
        for s, status, detail, _ in results:
            c = get_or_create_council(db, s.gss_code, s.name)
            if status == "ok":
                _mark_success(c)
            else:
                c.status, c.last_error = CouncilStatus.broken, detail
        db.commit()
    finally:
        db.close()

    ok = [r for r in results if r[1] == "ok"]
    bad = [r for r in results if r[1] != "ok"]
    print(f"\n{len(ok)}/{len(results)} councils working.")
    if bad:
        print("\nFailures by cause:")
        for kind, n in Counter(_kind(r[2]) for r in bad).most_common():
            names = ", ".join(r[0].name for r in bad if _kind(r[2]) == kind)
            print(f"  {n:3}  {kind}: {names[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
