"""
The UK council map on the home page: every council shaded by how many residents it has, the
visitor's own council picked out from their IP address, and the top 10.

Two prepared assets, built by app.tools.build_map on the server (it's the only machine that can
reach the sources) and kept in the database so they survive deploys:
  uk_map_svg     every council's outline (ONS Local Authority Districts, ultra-generalised,
                 Open Government Licence), projected and simplified to one small SVG
  ip_councils    UK IP ranges -> council, from DB-IP's free City Lite file (CC BY 4.0), with
                 each range's town placed in the council whose outline contains it

The IP lookup happens on our server: no visitor's address goes to anyone else, and the guess
isn't stored. It's a town-level guess and wrong for some people (mobile networks especially).
"""
import array
import bisect
import ipaddress
import json
import math
import struct
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

MAGIC = b"BJIP1"
LAT0 = math.radians(54.5)
SCALE = 100.0  # px per degree of latitude
LON_MIN, LAT_MAX = -8.75, 60.95  # top-left of the projection (covers Shetland and Fermanagh)


# ------------------------------------------------------------------ building (server-side tool)

def project(lon: float, lat: float) -> tuple[float, float]:
    """Equirectangular, squeezed east-west for the UK's latitude: simple and good enough at this size."""
    return (lon - LON_MIN) * math.cos(LAT0) * SCALE, (LAT_MAX - lat) * SCALE


def _rings(geom: dict) -> list[list[list[list[float]]]]:
    """Polygons as [exterior, *holes], each ring a list of [lon, lat]."""
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return list(geom["coordinates"])
    return []


def _fields(props: dict) -> tuple[str, str]:
    """ONS renames its columns every release (LAD24CD, LAD26CD...): find the code and name."""
    code = next(v for k, v in props.items() if k.upper().startswith("LAD") and k.upper().endswith("CD"))
    name = next(v for k, v in props.items() if k.upper().startswith("LAD") and k.upper().endswith("NM"))
    return code, name


def councils_from_geojson(gj: dict) -> list[dict]:
    out = []
    for f in gj.get("features", []):
        code, name = _fields(f.get("properties") or {})
        polys = _rings(f["geometry"])
        lons = [p[0] for poly in polys for p in poly[0]]
        lats = [p[1] for poly in polys for p in poly[0]]
        if not lons:
            continue
        out.append({"gss": code, "name": name, "polys": polys,
                    "bbox": (min(lons), min(lats), max(lons), max(lats))})
    return out


def build_svg(councils: list[dict]) -> str:
    paths, maxx, maxy = [], 0.0, 0.0
    for c in councils:
        parts = []
        for poly in c["polys"]:
            for ring in poly:
                pts, last = [], None
                for lon, lat in ring:
                    x, y = project(lon, lat)
                    p = (round(x), round(y))
                    maxx, maxy = max(maxx, x), max(maxy, y)
                    if p != last:
                        pts.append(p)
                        last = p
                if len(pts) >= 3:
                    parts.append("M" + " ".join(f"{x},{y}" for x, y in pts) + "Z")
        if parts:
            name = c["name"].replace("&", "&amp;").replace('"', "&quot;")
            paths.append(f'<path data-gss="{c["gss"]}" data-name="{name}" d="{"".join(parts)}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {math.ceil(maxx)} {math.ceil(maxy)}">'
            + "".join(paths) + "</svg>")


def _inside(lon: float, lat: float, ring: list[list[float]]) -> bool:
    hit, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def council_at(councils: list[dict], lon: float, lat: float) -> str | None:
    """The council whose outline contains the point; else the nearest one (a town on the coast
    can fall just outside the simplified outline)."""
    for c in councils:
        x0, y0, x1, y1 = c["bbox"]
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            for poly in c["polys"]:
                if _inside(lon, lat, poly[0]) and not any(_inside(lon, lat, h) for h in poly[1:]):
                    return c["gss"]
    best, dist = None, 0.25  # degrees: further than ~25km out to sea isn't the UK we know
    for c in councils:
        x0, y0, x1, y1 = c["bbox"]
        d = math.hypot(max(x0 - lon, 0, lon - x1), max(y0 - lat, 0, lat - y1))
        if d < dist:
            best, dist = c["gss"], d
    return best


def build_ip_table(rows, councils: list[dict]) -> bytes:
    """rows: (ip_start, ip_end, country, lat, lon) strings from DB-IP City Lite. UK rows only are
    kept, each town placed once, neighbouring ranges in the same council merged."""
    at: dict[tuple[str, str], str | None] = {}
    v4: list[list] = []
    v6: list[list] = []
    for start, end, country, lat, lon in rows:
        if country != "GB":
            continue
        key = (lat, lon)
        if key not in at:
            try:
                at[key] = council_at(councils, float(lon), float(lat))
            except ValueError:
                at[key] = None
        gss = at[key]
        if not gss:
            continue
        a, b = ipaddress.ip_address(start), ipaddress.ip_address(end)
        if a.version == 4:
            s, e, dst = int(a), int(b), v4
        else:
            s, e, dst = int(a) >> 64, int(b) >> 64, v6
        if dst and dst[-1][2] == gss and dst[-1][1] + 1 >= s:
            dst[-1][1] = max(dst[-1][1], e)
        else:
            dst.append([s, e, gss])
    codes = sorted({r[2] for r in v4 + v6})
    idx = {g: i for i, g in enumerate(codes)}
    out = [MAGIC, struct.pack("<H", len(codes)), b"".join(g.encode().ljust(9, b" ") for g in codes)]
    v4.sort()
    v6.sort()
    out.append(struct.pack("<I", len(v4)))
    out += [struct.pack("<IIH", s, e, idx[g]) for s, e, g in v4]
    out.append(struct.pack("<I", len(v6)))
    out += [struct.pack("<QQH", s, e, idx[g]) for s, e, g in v6]
    return b"".join(out)


# ------------------------------------------------------------------ runtime

class IpTable:
    def __init__(self, blob: bytes):
        if not blob.startswith(MAGIC):
            raise ValueError("not an ip_councils table")
        o = len(MAGIC)
        (n,) = struct.unpack_from("<H", blob, o); o += 2
        self.codes = [blob[o + 9 * i:o + 9 * i + 9].decode().strip() for i in range(n)]; o += 9 * n
        self.s4, self.e4, self.i4 = array.array("I"), array.array("I"), array.array("H")
        (n4,) = struct.unpack_from("<I", blob, o); o += 4
        for s, e, i in struct.iter_unpack("<IIH", blob[o:o + 10 * n4]):
            self.s4.append(s); self.e4.append(e); self.i4.append(i)
        o += 10 * n4
        self.s6, self.e6, self.i6 = array.array("Q"), array.array("Q"), array.array("H")
        (n6,) = struct.unpack_from("<I", blob, o); o += 4
        for s, e, i in struct.iter_unpack("<QQH", blob[o:o + 18 * n6]):
            self.s6.append(s); self.e6.append(e); self.i6.append(i)

    def lookup(self, ip: str) -> str | None:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
        if a.version == 4:
            v, s, e, ix = int(a), self.s4, self.e4, self.i4
        else:
            v, s, e, ix = int(a) >> 64, self.s6, self.e6, self.i6
        k = bisect.bisect_right(s, v) - 1
        return self.codes[ix[k]] if k >= 0 and v <= e[k] else None


_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}
ASSET_TTL = 3600
BOARD_TTL = 300


def _asset(db: Session, name: str):
    from app.models import Asset

    with _lock:
        hit = _cache.get(name)
        if hit and time.monotonic() - hit[0] < ASSET_TTL:
            return hit[1]
    row = db.get(Asset, name)
    val = None
    if row is not None:
        val = IpTable(row.data) if name == "ip_councils" else row.data.decode()
    with _lock:
        _cache[name] = (time.monotonic(), val)
    return val


def save_asset(db: Session, name: str, data: bytes) -> None:
    from app.models import Asset

    row = db.get(Asset, name) or Asset(name=name)
    row.data, row.updated_at = data, datetime.now(timezone.utc)
    db.add(row)
    db.flush()
    with _lock:
        _cache.pop(name, None)


def map_svg(db: Session) -> str | None:
    return _asset(db, "uk_map_svg")


def council_for_ip(db: Session, ip: str) -> str | None:
    table = _asset(db, "ip_councils")
    return table.lookup(ip) if table else None


def _counts(db: Session) -> list[dict]:
    from app.models import Council, Property, User, UserStatus

    with _lock:
        hit = _cache.get("board")
        if hit and time.monotonic() - hit[0] < BOARD_TTL:
            return hit[1]
    rows = db.execute(
        select(Council.gss_code, Council.name, func.count(User.id))
        .join(Property, Property.council_id == Council.id)
        .join(User, User.property_id == Property.id)
        .where(User.status == UserStatus.active, func.coalesce(User.lookup_source, "") != "seed")
        .group_by(Council.gss_code, Council.name)).all()
    board = sorted(({"gss": g, "name": n, "count": c} for g, n, c in rows if c),
                   key=lambda r: (-r["count"], r["name"]))
    with _lock:
        _cache["board"] = (time.monotonic(), board)
    return board


def summary(db: Session, ip: str) -> dict:
    """What the home page map shows: every council's count, the top 10, and the visitor's council."""
    board = _counts(db)
    counts = {r["gss"]: r["count"] for r in board}
    you = None
    gss = council_for_ip(db, ip)
    if gss:
        count = counts.get(gss, 0)
        name = next((r["name"] for r in board if r["gss"] == gss), None) or _name_on_map(db, gss)
        rank = (1 + sum(1 for r in board if r["count"] > count)) if count else None
        you = {"gss": gss, "name": name, "count": count, "rank": rank}
    return {"counts": counts, "top": board[:10], "you": you}


def _name_on_map(db: Session, gss: str) -> str | None:
    """A council nobody has joined from yet has no row of ours: its name from the map."""
    from app.models import Council

    c = db.scalar(select(Council.name).where(Council.gss_code == gss))
    if c:
        return c
    svg = map_svg(db) or ""
    i = svg.find(f'data-gss="{gss}" data-name="')
    if i < 0:
        return None
    i += len(f'data-gss="{gss}" data-name="')
    return svg[i:svg.find('"', i)].replace("&quot;", '"').replace("&amp;", "&")


def reset_cache() -> None:
    with _lock:
        _cache.clear()


__all__ = ["build_svg", "build_ip_table", "councils_from_geojson", "summary", "map_svg",
           "council_for_ip", "save_asset", "reset_cache", "IpTable", "json"]
