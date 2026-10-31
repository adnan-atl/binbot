"""Postcode -> council (GSS code) and postcode -> addresses (UPRN)."""
import re
from dataclasses import dataclass

import httpx

from app.core.config import get_settings

_PC = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})$")


class BadPostcode(ValueError):
    pass


def normalise_postcode(raw: str) -> str:
    s = re.sub(r"\s+", "", raw.upper())
    m = _PC.match(s)
    if not m:
        raise BadPostcode(raw)
    return f"{m.group(1)} {m.group(2)}"


@dataclass
class PostcodeInfo:
    postcode: str
    gss_code: str
    district: str
    lat: float | None
    lng: float | None


@dataclass
class AddressHit:
    uprn: str | None
    line: str
    house_number: str | None


def lookup_postcode(postcode: str) -> PostcodeInfo:
    s = get_settings()
    pc = normalise_postcode(postcode)
    if s.demo_mode:
        from app.services.demo import demo_postcode

        return demo_postcode(pc)
    r = httpx.get(f"{s.postcodes_io_url}/postcodes/{pc.replace(' ', '%20')}", timeout=10)
    if r.status_code == 404:
        raise BadPostcode(pc)
    r.raise_for_status()
    res = r.json()["result"]
    return PostcodeInfo(
        postcode=res["postcode"],
        gss_code=res["codes"]["admin_district"],
        district=res["admin_district"],
        lat=res.get("latitude"),
        lng=res.get("longitude"),
    )


def _paon(line: str) -> str | None:
    """Best-effort house number / name from the first address line."""
    m = re.match(r"^\s*((?:flat|apartment|unit)\s*\S+[, ]+)?(\d+[A-Za-z]?)", line, re.I)
    if m:
        return (m.group(1) or "").strip(", ") + (" " if m.group(1) else "") + m.group(2)
    return line.split(",")[0].strip() or None


def os_door(dpa: dict) -> str | None:
    """
    What a council search box expects as "house number": the street door, not the flat.
    OS Places splits the address for us, so use its fields instead of parsing the string:
    "FLAT 3, 18 ELM ROAD" -> "18"; "ROSE COTTAGE, ELM ROAD" -> "ROSE COTTAGE".
    """
    num = (dpa.get("BUILDING_NUMBER") or "").strip()
    name = (dpa.get("BUILDING_NAME") or "").strip()
    return num or name or _paon(dpa.get("ADDRESS", ""))


def lookup_addresses(postcode: str) -> list[AddressHit]:
    """
    Addresses with UPRNs for a postcode. Provider-dependent:
      os_places  - Ordnance Survey Places API (returns UPRN; free tier exists)
      getaddress - getAddress.io (returns UPRN on the 'expand' endpoint)
      none       - no provider; the user types a house number and councils that need a
                   UPRN become 'manual' until a provider is configured.
    """
    s = get_settings()
    pc = normalise_postcode(postcode)
    if s.demo_mode:
        from app.services.demo import demo_addresses

        return demo_addresses(pc)
    if s.address_provider == "os_places" and s.os_places_api_key:
        r = httpx.get(
            "https://api.os.uk/search/places/v1/postcode",
            params={"postcode": pc, "key": s.os_places_api_key, "dataset": "DPA"},
            timeout=10,
        )
        r.raise_for_status()
        out = []
        for item in r.json().get("results", []):
            d = item.get("DPA", {})
            line = d.get("ADDRESS", "")
            out.append(AddressHit(uprn=str(d.get("UPRN")), line=line, house_number=os_door(d)))
        return out
    if s.address_provider == "getaddress" and s.getaddress_api_key:
        r = httpx.get(
            f"https://api.getaddress.io/find/{pc.replace(' ', '')}",
            params={"api-key": s.getaddress_api_key, "expand": "true"},
            timeout=10,
        )
        r.raise_for_status()
        out = []
        for a in r.json().get("addresses", []):
            line = ", ".join(x for x in a.get("formatted_address", []) if x)
            out.append(
                AddressHit(uprn=str(a["uprn"]) if a.get("uprn") else None, line=line,
                           house_number=a.get("building_number") or _paon(line))
            )
        return out
    return []
