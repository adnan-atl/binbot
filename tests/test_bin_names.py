from app.services.normalize import bin_emoji, bin_name, bin_tag


def test_colour_first_kind_in_brackets_and_capitals():
    assert bin_name("Mixed dry recycling (blue lidded bin)") == "Blue lidded bin (Mixed dry recycling)"
    assert bin_name("glass (black box or basket)") == "Black box or basket (Glass)"
    assert bin_name("Household waste") == "Household waste"
    assert bin_name("food waste (weekly)") == "Food waste (weekly)"  # bracket isn't a colour


def test_household_waste_is_black_like_the_website():
    assert bin_emoji("Household waste", "general") == "⚫"
    assert bin_tag({"label": "glass (black box or basket)", "category": "glass"}) == \
        "⚫ Black box or basket (Glass)"
    assert bin_emoji("Glass", "glass") == "\U0001F37E"  # no colour anywhere: the kind of bin
