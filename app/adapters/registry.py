"""
Council registry: which adapter serves which local authority, and what it needs.

Built from UKBinCollectionData's tests/input.json, which lists every council module with
its LAD24CD (GSS code) and sample parameters. We read it from the installed package so
`pip install -U uk-bin-collection` picks up new councils with no code change.
"""
import importlib.metadata
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class AdapterSpec:
    module: str
    gss_code: str
    name: str
    url: str
    skip_get_url: bool = False
    needs_uprn: bool = False
    needs_postcode: bool = False
    needs_house_number: bool = False
    needs_browser: bool = False
    note: str = ""
    params: dict = field(default_factory=dict)
    placeholder: str = ""  # the X-run in the URL that the UPRN replaces
    sample_id: str = ""  # the UPRN in the library's sample URL (for the sweep)
    needs_council_id: bool = False  # URL wants the council's own property id, not a UPRN


def _url_id(v: dict) -> tuple[str, str, bool]:
    """
    (placeholder, sample id, needs_council_id) from the URL template. Some modules put the
    UPRN in the URL instead of taking a uprn argument ('/property/XXXXXXXXXX'); input.json
    then has no "uprn" key, so without this we never asked for one and the URL went out
    with a literal XXXX in it. Templates with a council-specific id (Bromley, Kingston,
    Antrim...) can't be filled from an address lookup and are left unsupported.
    """
    tpl = v.get("wiki_command_url_override") or ""
    runs = re.findall(r"X{4,}", tpl)
    if not runs:
        return "", "", False
    ph = runs[0]
    i = tpl.find(ph)
    before = tpl[:i].lower()
    uprn = len(runs) == 1 and (len(ph) == 8 or before.endswith(("uprn=", "/property/")))
    if not uprn:
        return "", "", True
    prefix, suffix, sample = tpl[:i], tpl[i + len(ph):], v.get("url", "")
    sid = ""
    if sample.startswith(prefix) and (not suffix or sample.endswith(suffix)):
        sid = sample[len(prefix):len(sample) - len(suffix) if suffix else None]
    return ph, sid, False


def _input_json_path() -> Path:
    import uk_bin_collection

    root = Path(list(uk_bin_collection.__path__)[0])
    return root / "tests" / "input.json"


def library_version() -> str:
    try:
        return importlib.metadata.version("uk-bin-collection")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@lru_cache
def load_specs() -> dict[str, AdapterSpec]:
    """gss_code -> AdapterSpec. Councils without a LAD code are skipped (can't be routed)."""
    data = json.loads(_input_json_path().read_text())
    specs: dict[str, AdapterSpec] = {}
    for module, v in data.items():
        gss = v.get("LAD24CD") or MISSING_LAD.get(module)
        if not gss:
            continue
        spec = _spec(module, v, gss)
        if spec.needs_council_id:
            continue
        # A few modules cover several councils.
        for extra in v.get("supported_councils_LAD24CD", []) or []:
            specs.setdefault(extra, spec)
        specs.setdefault(gss, spec)
    return specs


@lru_cache(maxsize=None)
def _code_needs(module: str) -> frozenset[str]:
    """
    What the scraper actually reads, from its source: kwargs.get("uprn"), check_paon(...).
    input.json's sample keys miss ~30 of these (Brighton's uprn, Haringey's house number,
    Westminster's postcode...), and every miss was a council that silently got no input
    and failed. The code is the ground truth; input.json is just one example call.
    """
    import uk_bin_collection

    f = Path(list(uk_bin_collection.__path__)[0]) / "uk_bin_collection" / "councils" / f"{module}.py"
    try:
        src = f.read_text()
    except OSError:
        return frozenset()
    keys = set(re.findall(r"kwargs(?:\.get\(|\[)\s*[\"'](\w+)[\"']", src))
    for fn, key in (("check_uprn(", "uprn"), ("check_paon(", "paon"), ("check_postcode(", "postcode")):
        if fn in src:
            keys.add(key)
    return frozenset(keys)


def _spec(module: str, v: dict, gss: str) -> AdapterSpec:
    ph, sid, council_id = _url_id(v)
    code = _code_needs(module)
    return AdapterSpec(
            placeholder=ph,
            sample_id=sid,
            needs_council_id=council_id,
            module=module,
            gss_code=gss,
            name=v.get("wiki_name", module),
            url=v.get("wiki_command_url_override") or v.get("url", ""),
            skip_get_url=bool(v.get("skip_get_url", False)),
            needs_uprn="uprn" in v or bool(ph) or "uprn" in code,
            needs_postcode="postcode" in v or "postcode" in code,
            needs_house_number="house_number" in v or "paon" in v
            or bool(code & {"paon", "number", "house_number"}),
            needs_browser="web_driver" in v,
            note=v.get("wiki_note", ""),
            params={k: v[k] for k in ("url", "skip_get_url") if k in v},
        )


# Library modules that ship without a LAD24CD in input.json, so they'd never be routed and
# the council would look "unsupported". Codes are ONS LAD codes.
MISSING_LAD = {
    "WestminsterCityCouncil": "E09000033",
    "BridgendCountyBoroughCouncil": "W06000013",
    "CaerphillyCountyBoroughCouncil": "W06000018",
    "ClackmannanshireCouncil": "S12000005",
    "CneSiarCouncil": "S12000013",
    "EastHampshireDC": "E07000085",
    "InverclydeCouncil": "S12000018",
    "IsleOfWightCouncil": "E06000046",
    "MerthyrTydfilCountyBoroughCouncil": "W06000024",
    "OrkneyIslandsCouncil": "S12000023",
    "ScottishBordersCouncil": "S12000026",
    "ShetlandIslandsCouncil": "S12000027",
    "SouthendOnSeaCityCouncil": "E06000033",
    "TamworthBoroughCouncil": "E07000199",
    "TorfaenCouncil": "W06000020",
}

_NOISE = re.compile(r"\b(london|royal|borough|city|county|district|metropolitan|council|"
                    r"of|the|and|dc|bc|cc|mbc)\b")


def _norm(name: str) -> str:
    """'Royal Borough of Kingston upon Thames' / 'KingstonUponThamesCouncil' -> 'kingstonuponthames'."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).lower().replace("&", " and ")
    return re.sub(r"[^a-z]", "", _NOISE.sub(" ", spaced))


@lru_cache
def _by_name() -> dict[str, AdapterSpec]:
    data = json.loads(_input_json_path().read_text())
    out: dict[str, AdapterSpec] = {}
    for module, v in data.items():
        spec = _spec(module, v, v.get("LAD24CD") or MISSING_LAD.get(module, ""))
        if spec.needs_council_id:
            continue
        for n in (module, v.get("wiki_name", "")):
            if n and _norm(n):
                out.setdefault(_norm(n), spec)
    return out


def spec_for(gss_code: str, name: str | None = None) -> AdapterSpec | None:
    """By GSS code; failing that, by council name (postcodes.io district name) so a module
    whose input.json entry lacks or has a stale LAD code still gets used."""
    spec = load_specs().get(gss_code)
    if spec is None and name:
        spec = _by_name().get(_norm(name))
    return spec


def coverage() -> dict:
    specs = load_specs()
    return {
        "library_version": library_version(),
        "councils": len(specs),
        "need_browser": sum(1 for s in specs.values() if s.needs_browser),
        "need_uprn": sum(1 for s in specs.values() if s.needs_uprn),
    }
