"""Canned data for DEMO_MODE (no network) and for tests."""
from datetime import date, timedelta

from app.adapters.base import RawCollection
from app.services.geo import AddressHit, PostcodeInfo

DEMO_GSS = "E09000012"  # Hackney


def demo_postcode(pc: str) -> PostcodeInfo:
    return PostcodeInfo(postcode=pc, gss_code=DEMO_GSS, district="Hackney", lat=51.54, lng=-0.06)


def demo_addresses(pc: str) -> list[AddressHit]:
    return [
        AddressHit(uprn=f"1000{i:04d}", line=f"{n}, Elm Road, London, {pc}", house_number=str(n))
        for i, n in enumerate((10, 12, 14, 16), start=1)
    ] + [
        AddressHit(uprn="10000099", line=f"Flat 3, 18 Elm Road, London, {pc}",
                   house_number="Flat 3, 18")
    ]


def demo_collections(today: date | None = None) -> list[RawCollection]:
    """Tuesday round: recycling + general alternate fortnightly, food weekly, garden 4-weekly."""
    today = today or date.today()
    tue = today + timedelta(days=(1 - today.weekday()) % 7)
    out: list[RawCollection] = []
    for i in range(12):
        d = tue + timedelta(weeks=i)
        out.append(RawCollection("Food waste caddy", d))
        out.append(RawCollection("Blue recycling bin" if i % 2 == 0 else "Black general waste bin", d))
        if i % 4 == 2:
            out.append(RawCollection("Green garden waste bin", d))
    return out
