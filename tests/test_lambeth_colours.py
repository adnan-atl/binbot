"""Lambeth names containers by colour only; its own records say green is recycling and brown
is the food caddy."""
from sqlalchemy import select

from app.models import BinCategory, BinType, User, UserBinSetting
from app.services.normalize import categorise
from app.services.schedule import get_or_create_bin_type, get_or_create_council

LAMBETH = "E09000022"


def test_lambeth_colours():
    assert categorise("green bin", LAMBETH) == BinCategory.recycling
    assert categorise("brown bin", LAMBETH) == BinCategory.food
    assert categorise("brown food caddy", LAMBETH) == BinCategory.food
    assert categorise("black bin", LAMBETH) == BinCategory.general
    assert categorise("garden waste sacks", LAMBETH) == BinCategory.garden
    assert categorise("brown bin") == BinCategory.garden  # everywhere else


def test_bins_filed_wrongly_before_are_corrected_on_the_next_lookup(db):
    council = get_or_create_council(db, LAMBETH, "Lambeth")
    bt = BinType(council_id=council.id, council_label="brown bin", category=BinCategory.garden,
                 colour="brown")
    user = User(referral_code="lam00001")
    db.add_all([bt, user])
    db.flush()
    st = UserBinSetting(user_id=user.id, bin_type_id=bt.id, label="Brown bin",
                        category=BinCategory.garden)
    db.add(st)
    db.flush()
    assert get_or_create_bin_type(db, council, "brown bin").id == bt.id
    assert bt.category == BinCategory.food and st.category == BinCategory.food
