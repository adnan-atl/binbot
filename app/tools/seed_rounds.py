"""Load hand-copied calendars (app/data/manual_rounds.json) for councils we can't ask.

    docker exec binny-api-1 python -m app.tools.seed_rounds

Each postcode gets a placeholder resident (no phone, never messaged, left out of the daily
report) holding the calendar, so real sign-ups on that postcode get it straight away
(signup.copy_neighbour_manual). Safe to run again: a postcode's calendar is replaced."""
import json
from pathlib import Path

from sqlalchemy import select

from app.core.db import session_factory
from app.models import BinCategory, Property, User, UserBinSetting, UserStatus
from app.services import holidays
from app.services.schedule import resolve_property
from app.services.signup import new_referral_code

SEED = "seed"


def load(db, rounds: list[dict]) -> list[str]:
    done = []
    for r in rounds:
        prop = resolve_property(db, r["postcode"])
        user = db.scalar(select(User).join(Property, Property.id == User.property_id)
                         .where(Property.round_key == prop.round_key, User.lookup_source == SEED))
        if user is None:
            user = User(referral_code=new_referral_code(), status=UserStatus.pending,
                        lookup_source=SEED, property_id=prop.id)
            db.add(user)
            db.flush()
        for s in db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == user.id)):
            db.delete(s)
        gss = prop.council.gss_code
        for b in r["bins"]:
            db.add(UserBinSetting(
                user_id=user.id, label=b["label"], category=BinCategory(b["category"]),
                colour=b.get("colour"),
                override_rule={"days": b["days"], "frequency": b["frequency"],
                               "anchor": b.get("anchor"),
                               "moves": holidays.council_moves(gss, b["category"])}))
        db.flush()
        done.append(f"{prop.postcode} -> {prop.round_key} ({len(r['bins'])} bins)")
    return done


def main() -> None:
    path = Path(__file__).resolve().parent.parent / "data" / "manual_rounds.json"
    rounds = json.loads(path.read_text(encoding="utf-8"))["rounds"]
    db = session_factory()()
    try:
        for line in load(db, rounds):
            print(line)
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
