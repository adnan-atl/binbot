"""What goes between the api/worker and the scraper container: the adapter spec and the home
in, collections or a typed error out. Plain JSON, so neither side unpickles anything."""
from dataclasses import asdict, fields
from datetime import date

from app.adapters.base import AdapterError, LookupInput, MissingInput, NoCollectionsForAddress, RawCollection
from app.adapters.registry import AdapterSpec


def spec_to_json(spec: AdapterSpec) -> dict:
    d = asdict(spec)
    d["requires"] = sorted(spec.requires)
    return d


def spec_from_json(d: dict) -> AdapterSpec:
    known = {f.name for f in fields(AdapterSpec)}
    d = {k: v for k, v in d.items() if k in known}
    d["requires"] = frozenset(d.get("requires") or ())
    return AdapterSpec(**d)


def lookup_to_json(lookup: LookupInput) -> dict:
    return asdict(lookup)


def lookup_from_json(d: dict) -> LookupInput:
    return LookupInput(postcode=d["postcode"], uprn=d.get("uprn"), house_number=d.get("house_number"),
                       address_line=d.get("address_line"))


def result_to_json(items: list[RawCollection]) -> dict:
    return {"ok": True, "collections": [{"label": c.label, "date": c.date.isoformat()} for c in items]}


def error_to_json(e: AdapterError) -> dict:
    kind = ("missing" if isinstance(e, MissingInput) else
            "no_collections" if isinstance(e, NoCollectionsForAddress) else "adapter")
    return {"ok": False, "kind": kind, "message": str(e), "retryable": e.retryable,
            "field": getattr(e, "field", None)}


def result_from_json(d: dict) -> list[RawCollection]:
    if d.get("ok"):
        return [RawCollection(label=c["label"], date=date.fromisoformat(c["date"])) for c in d["collections"]]
    kind, msg = d.get("kind"), d.get("message") or "scraper failed"
    if kind == "missing":
        raise MissingInput(d.get("field") or "input")
    if kind == "no_collections":
        raise NoCollectionsForAddress(msg)
    raise AdapterError(msg, retryable=bool(d.get("retryable", True)))
