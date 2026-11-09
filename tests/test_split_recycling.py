"""Councils whose one "Recycling" collection goes out in several coloured containers
(Hounslow: red box, green box for glass, blue box for paper): never one colour for them all."""
from app.services.normalize import bin_tag, colours_text, split_recycling

HOUNSLOW, HACKNEY = "E09000018", "E09000012"


def test_hounslow_has_three_containers():
    assert split_recycling(HOUNSLOW) == [("red", ""), ("green", "glass"), ("blue", "paper and card")]
    assert colours_text(split_recycling(HOUNSLOW)) == "red, green and blue"
    assert colours_text(split_recycling(HOUNSLOW), detail=True) == \
        "red, green (glass) and blue (paper and card)"


def test_one_colour_councils_are_untouched():
    assert split_recycling(HACKNEY) == [] and split_recycling(None) == [] and split_recycling("_about") == []


def test_message_names_every_colour_with_the_recycling_symbol():
    b = {"label": "Recycling", "category": "recycling", "colour": None, "colours": ["red", "green", "blue"]}
    assert bin_tag(b) == "♻️ Recycling (red, green and blue)"
    # A colour the resident (or the council's own label) gave still wins
    assert bin_tag({**b, "colour": "red"}) == "\U0001F534 Recycling"
    # Old reminders stored without the field keep working
    assert bin_tag({"label": "Recycling", "category": "recycling", "colour": "blue"}) == "\U0001F535 Recycling"


def test_schedule_and_reminders_use_it_unless_glass_has_its_own_bin(db):
    from types import SimpleNamespace as NS

    from app.models import BinCategory
    from app.services import schedule

    council = NS(gss_code=HOUNSLOW)
    bt = NS(category=BinCategory.recycling, colour=None, council=council, id="bt1")
    orig = schedule.resident_colour
    schedule.resident_colour = lambda db_, bt_, rk=None: None
    try:
        assert [c for c, _ in schedule.recycling_containers(db, bt, "r", {"recycling", "general"})] == \
            ["red", "green", "blue"]
        assert schedule.recycling_containers(db, bt, "r", {"recycling", "glass"}) == []
        named = NS(category=BinCategory.recycling, colour="red", council=council, id="bt2")
        assert schedule.recycling_containers(db, named, "r", {"recycling"}) == []
        schedule.resident_colour = lambda db_, bt_, rk=None: "red"
        assert schedule.recycling_containers(db, bt, "r", {"recycling"}) == []
    finally:
        schedule.resident_colour = orig
