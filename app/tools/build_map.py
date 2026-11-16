"""Build the home page map's two assets and store them in the database (services/uk_map.py).

    docker exec binny-api-1 python -m app.tools.build_map

Downloads the ONS council outlines (newest UK ultra-generalised release) and DB-IP's free City
Lite file for this month (or last month, early on), keeps UK rows only and places each town in
its council. Takes a few minutes and ~100MB of download. Run again monthly to keep the IP
guesses fresh. Messages nobody."""
import csv
import gzip
import io
import re
import sys
from datetime import date

import httpx

from app.core.db import session_factory
from app.services import uk_map

ONS = "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services"
UA = {"User-Agent": "Binjamin/1.0 (+https://binjamin.uk)"}


def newest_boundaries(http: httpx.Client) -> str:
    names = [s["name"] for s in http.get(ONS, params={"f": "json"}).json().get("services", [])]
    pat = re.compile(r"Local_Authority_Districts_(January|February|March|April|May|June|July|August|"
                     r"September|October|November|December)_(\d{4})_Boundaries_+UK_BUC$")
    months = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]
    found = [(int(m.group(2)), months.index(m.group(1)), n) for n in names if (m := pat.match(n))]
    if not found:
        raise SystemExit("no ONS council boundaries found")
    return max(found)[2]


def fetch_geojson(http: httpx.Client, service: str) -> dict:
    url = f"{ONS}/{service}/FeatureServer/0/query"
    feats, offset = [], 0
    while True:
        r = http.get(url, params={"where": "1=1", "outFields": "*", "outSR": "4326", "f": "geojson",
                                  "resultOffset": offset, "resultRecordCount": 200})
        r.raise_for_status()
        page = r.json().get("features", [])
        feats += page
        if len(page) < 200:
            return {"type": "FeatureCollection", "features": feats}
        offset += 200


def dbip_rows(http: httpx.Client):
    today = date.today()
    for y, m in ((today.year, today.month), (today.year - (today.month == 1), (today.month - 2) % 12 + 1)):
        url = f"https://download.db-ip.com/free/dbip-city-lite-{y}-{m:02d}.csv.gz"
        with http.stream("GET", url) as r:
            if r.status_code != 200:
                continue
            raw = io.BytesIO(b"".join(r.iter_bytes()))
        for row in csv.reader(io.TextIOWrapper(gzip.GzipFile(fileobj=raw), encoding="utf-8")):
            if len(row) >= 8:
                yield row[0], row[1], row[3], row[6], row[7]
        return
    raise SystemExit("no DB-IP City Lite file this month or last")


def main() -> None:
    http = httpx.Client(headers=UA, timeout=120, follow_redirects=True)
    service = newest_boundaries(http)
    councils = uk_map.councils_from_geojson(fetch_geojson(http, service))
    print(f"{service}: {len(councils)} councils")
    svg = uk_map.build_svg(councils)
    table = uk_map.build_ip_table(dbip_rows(http), councils)
    t = uk_map.IpTable(table)
    print(f"map {len(svg) // 1024}KB; IP table {len(table) // 1024}KB, {len(t.s4)} IPv4 + {len(t.s6)} IPv6 ranges")
    if "--dry-run" in sys.argv:
        return
    db = session_factory()()
    try:
        uk_map.save_asset(db, "uk_map_svg", svg.encode())
        uk_map.save_asset(db, "ip_councils", table)
        db.commit()
        print("saved")
    finally:
        db.close()


if __name__ == "__main__":
    main()
