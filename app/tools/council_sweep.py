"""One-off: time every council's lookup with the scraper library's own test address
(tests/input.json), to find councils that are slow, blocked or broken.

    docker exec -d binny-api-1 python -m app.tools.council_sweep   # ~15-30 min, in the background
    docker exec binny-api-1 cat /tmp/council_sweep.txt             # the summary when it's done

Writes /tmp/council_sweep.csv (one row per council) and /tmp/council_sweep.txt (the summary).
Goes through the scraper container like a real sign-up, so the proxy fallback counts too.
Polite: a few at a time, one lookup per council."""
import concurrent.futures
import csv
import json
import time

from app.adapters.base import AdapterError, LookupInput, unreachable
from app.adapters.registry import _input_json_path, load_specs
from app.adapters.ukbcd import UKBCDAdapter

WORKERS = 6
DEADLINE = 100  # seconds per council, on top of the scraper's own limit


def sample(spec, data: dict) -> LookupInput | None:
    v = data.get(spec.module)
    if not v:
        return None
    uprn = v.get("uprn") or spec.sample_id or None
    house = v.get("house_number") or v.get("paon") or v.get("number")
    return LookupInput(postcode=v.get("postcode") or "", uprn=str(uprn) if uprn else None,
                       house_number=str(house) if house else None)


def check(gss: str, spec, lookup: LookupInput | None) -> dict:
    row = {"gss": gss, "council": spec.name, "module": spec.module,
           "browser": spec.needs_browser, "secs": "", "result": "", "error": ""}
    if lookup is None:
        row["result"] = "no test address"
        return row
    t = time.monotonic()
    try:
        items = UKBCDAdapter(spec).fetch(lookup, validate=False)
        row["result"] = f"ok ({len(items)} dates)"
    except AdapterError as e:
        row["result"] = "blocked/down" if unreachable(str(e)) else "failed"
        row["error"] = str(e)[:200]
    except Exception as e:  # noqa: BLE001 - a sweep never stops for one council
        row["result"], row["error"] = "failed", f"{type(e).__name__}: {e}"[:200]
    row["secs"] = round(time.monotonic() - t, 1)
    return row


def main() -> None:
    data = json.loads(_input_json_path().read_text())
    seen, jobs = set(), []
    for gss, spec in sorted(load_specs().items(), key=lambda kv: kv[1].name):
        if spec.module in seen:  # one module serving several councils: check it once
            continue
        seen.add(spec.module)
        jobs.append((gss, spec, sample(spec, data)))
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(check, *j): j for j in jobs}
        for f in concurrent.futures.as_completed(futs):
            gss, spec, _ = futs[f]
            try:
                rows.append(f.result(timeout=DEADLINE))
            except Exception as e:  # noqa: BLE001
                rows.append({"gss": gss, "council": spec.name, "module": spec.module,
                             "browser": spec.needs_browser, "secs": DEADLINE, "result": "hung",
                             "error": str(e)[:200]})
            with open("/tmp/council_sweep.progress", "w") as fh:
                fh.write(f"{len(rows)}/{len(jobs)}\n")
    rows.sort(key=lambda r: (-(r["secs"] or 0)))
    with open("/tmp/council_sweep.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    ok = [r for r in rows if str(r["result"]).startswith("ok")]
    slow = [r for r in ok if (r["secs"] or 0) > 20]
    bad = [r for r in rows if r["result"] in ("blocked/down", "hung")]
    failed = [r for r in rows if r["result"] == "failed"]
    lines = [f"{len(rows)} scrapers checked: {len(ok)} ok, {len(slow)} of them slow (>20s), "
             f"{len(bad)} blocked/down, {len(failed)} failed, "
             f"{sum(1 for r in rows if r['result'] == 'no test address')} without a test address", ""]
    for title, group in (("SLOW (works, >20s)", slow), ("BLOCKED / DOWN", bad), ("FAILED", failed)):
        lines.append(f"== {title}: {len(group)}")
        lines += [f"{r['secs']:>6}s  {r['council'][:34]:34}  {'browser ' if r['browser'] else ''}{r['error'][:90]}"
                  for r in group]
        lines.append("")
    with open("/tmp/council_sweep.txt", "w") as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines[:1]))


if __name__ == "__main__":
    main()
