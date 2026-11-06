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


def test_non_recyclable_refuse_is_general_and_mixed_recycling_with_glass_is_recycling():
    from app.services.normalize import categorise
    assert categorise("Non-Recyclable Refuse collection").value == "general"  # Bromley
    assert categorise("Non recyclable waste").value == "general"
    assert categorise("Mixed Recycling (Cans, Plastics & Glass) collection").value == "recycling"
    assert categorise("Glass recycling (black box)").value == "glass"
    assert categorise("glass (black box or basket)").value == "glass"
    assert categorise("Mixed dry recycling (blue lidded bin)").value == "recycling"


def test_everyday_bin_is_general_waste_black_bin_and_never_a_wastebasket():
    from app.services.normalize import bin_tag, categorise, display_label
    for council_name in ("Household waste", "Waste", "Rubbish collection", "Refuse",
                         "Non-Recyclable Refuse collection", "Residual waste"):
        cat = categorise(council_name).value
        assert display_label(council_name, cat) == "General waste (black bin)", council_name
        assert bin_tag({"label": council_name, "category": cat}) == "⚫ Black bin (General waste)"
    assert display_label("Green bin", "general") == "Green bin"  # Glasgow: its own colour stays
    assert display_label("glass (black box or basket)", "glass") == "Glass (black box or basket)"
    assert bin_tag({"label": "Something odd", "category": "other"}).startswith("⚫")  # a dot
