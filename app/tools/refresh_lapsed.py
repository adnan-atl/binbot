"""Ask the council again for rounds where a bin has run out of dates since the last lookup
(schedule.lapsed_bins), and give that bin to residents who joined in the meantime.

    docker exec binny-api-1 python -m app.tools.refresh_lapsed            # list only
    docker exec binny-api-1 python -m app.tools.refresh_lapsed --apply    # refresh and repair

The hourly refresh does the same once a round's weekly check comes round; this is for not
waiting. Messages nobody."""
import sys

from sqlalchemy import select

from app.adapters.registry import BLOCKED

from app.core.db import session_factory
from app.models import CouncilStatus, Property, User
from app.services import signup
from app.services.schedule import lapsed_bins, refresh_round, representative_for


def run(db, apply: bool) -> list[str]:
    out = []
    keys = sorted(set(db.scalars(select(Property.round_key)
                                 .join(User, User.property_id == Property.id)).all()))
    for key in keys:
        lapsed = lapsed_bins(db, key)
        if not lapsed:
            continue
        prop = representative_for(db, key)
        if (not prop or not prop.council or prop.council.status == CouncilStatus.unsupported
                or prop.council.gss_code in BLOCKED):
            continue
        line = f"{key}: {len(lapsed)} bin(s) out of dates"
        if apply:
            res = refresh_round(db, prop)
            if res.error:
                line += f" - council lookup failed: {res.error}"
            else:
                line += f" - refreshed, {signup.restore_lapsed_bins(db, key, lapsed)} bin(s) restored"
            db.commit()
        out.append(line)
    return out


def main() -> None:
    db = session_factory()()
    try:
        lines = run(db, "--apply" in sys.argv)
        print("\n".join(lines) if lines else "Nothing out of dates.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
