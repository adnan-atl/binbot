"""
Fetch the neighbours' calendars before they ask.

When someone's sign-up needed a fresh council lookup (nobody on their postcode had one yet),
look up the nearest other postcodes in the same council in the background, one at a time, so
the next people from that street or group chat load in a moment instead of waiting on the
council's website (Cotswold takes ~27s a lookup). Each postcode gets its own lookup, exactly as
if a resident there had signed up first, so nobody gets someone else's days.

Bounded: at most PREFETCH_NEIGHBOURS postcodes per trigger, within PREFETCH_RADIUS_M, one
browser at a time (live sign-ups keep the rest), only the free address provider, and rounds
nobody has joined aren't re-checked by the weekly refresh (monitor.refresh_stale).
"""
import logging
import threading

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import session_factory
from app.models import CouncilStatus, Property
from app.services import geo
from app.services.schedule import postcode_round_key, refresh_round, resolve_property

log = logging.getLogger(__name__)

PREFETCH_RADIUS_M = 500
SOURCE = "prefetch"
_one_at_a_time = threading.Lock()
_queued: set[str] = set()


def nearby_postcodes(postcode: str, gss_code: str, limit: int) -> list[str]:
    """Nearest postcodes to this one (postcodes.io), closest first, same council only."""
    s = get_settings()
    pc = geo.normalise_postcode(postcode)
    r = httpx.get(f"{s.postcodes_io_url}/postcodes/{pc.replace(' ', '%20')}/nearest",
                  params={"limit": 40, "radius": PREFETCH_RADIUS_M}, timeout=10)
    r.raise_for_status()
    out = []
    for res in r.json().get("result") or []:
        if res.get("postcode") != pc and (res.get("codes") or {}).get("admin_district") == gss_code:
            out.append(res["postcode"])
    return out[:limit]


def start(property_id: str) -> None:
    """Kick off a background prefetch for this home's neighbours (never blocks the sign-up)."""
    if get_settings().prefetch_neighbours <= 0 or property_id in _queued:
        return
    _queued.add(property_id)
    threading.Thread(target=run, args=(property_id,), daemon=True).start()


def run(property_id: str) -> int:
    """Look up the neighbouring postcodes that have no calendar yet. Returns how many worked."""
    s = get_settings()
    done = 0
    with _one_at_a_time:
        db = session_factory()()
        try:
            prop = db.get(Property, property_id)
            council = prop.council if prop else None
            if council is None or council.status == CouncilStatus.unsupported:
                return 0
            try:
                todo = nearby_postcodes(prop.postcode, council.gss_code, 40)
            except httpx.HTTPError as e:
                log.warning("prefetch: nearby postcodes for %s failed: %s", prop.postcode, e)
                return 0
            tried = 0
            for pc in todo:
                if tried >= s.prefetch_neighbours:
                    break
                key = postcode_round_key(council, pc)
                # Someone there already (a resident, or an earlier prefetch): nothing to do
                if db.scalar(select(Property.id).where(Property.round_key == key).limit(1)):
                    continue
                hits = [a for a in geo.lookup_addresses(pc, paid_ok=False) if a.uprn or a.house_number]
                if not hits:
                    continue
                tried += 1
                a = hits[0]
                p = resolve_property(db, pc, a.uprn, a.house_number, a.line)
                res = refresh_round(db, p, source=SOURCE)
                db.commit()
                if res.error:
                    log.info("prefetch: %s failed: %s", pc, res.error[:200])
                    continue
                done += 1
            log.info("prefetch: %s neighbours ready for %s", done, prop.postcode)
        except Exception:  # never surfaces to anyone; ops see it in the logs
            log.exception("prefetch failed for %s", property_id)
            db.rollback()
        finally:
            db.close()
            _queued.discard(property_id)
    return done
