"""A council bin most residents have unticked starts unticked for the next sign-up."""
from sqlalchemy import select

from app.models import User, UserBinSetting, UserStatus
from tests.test_flow import signup


def _residents(client, db, n, off):
    """n active residents on the demo round; the first `off` have switched their garden-ish bin off."""
    target = None
    for i in range(n):
        uid = signup(client)["user_id"]
        u = db.get(User, uid)
        u.status = UserStatus.active
        rows = db.scalars(select(UserBinSetting).where(UserBinSetting.user_id == uid)
                          .order_by(UserBinSetting.label)).all()
        target = target or rows[0].bin_type_id
        if i < off:
            next(s for s in rows if s.bin_type_id == target).enabled = False
    db.commit()
    return target


def _enabled(db, uid, bin_type):
    db.expire_all()
    return db.scalar(select(UserBinSetting.enabled).where(UserBinSetting.user_id == uid,
                                                          UserBinSetting.bin_type_id == bin_type))


def test_starts_unticked_once_most_residents_untick_it(client, db):
    bt = _residents(client, db, 10, off=6)
    assert _enabled(db, signup(client)["user_id"], bt) is False


def test_stays_ticked_below_the_share_or_with_too_few_residents(client, db):
    bt = _residents(client, db, 9, off=9)  # everyone, but only 9 residents
    assert _enabled(db, signup(client)["user_id"], bt) is True


def test_half_is_not_most(client, db):
    bt = _residents(client, db, 10, off=5)
    assert _enabled(db, signup(client)["user_id"], bt) is True
