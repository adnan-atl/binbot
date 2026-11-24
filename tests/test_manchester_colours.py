"""Manchester names bins by colour only: blue is paper and card, brown glass/cans/plastic,
green food and garden, black general. The general colour guesses got brown and green wrong."""
from app.models import BinCategory
from app.services.normalize import categorise, council_purpose

MCR = "E08000003"


def test_manchester_bins_mean_what_the_council_says():
    assert categorise("Blue bin", MCR) == BinCategory.paper
    assert categorise("Brown bin", MCR) == BinCategory.recycling
    assert categorise("Green Bin", MCR) == BinCategory.garden
    assert categorise("Black bin", MCR) == BinCategory.general
    assert council_purpose("Brown bin", MCR) == "Glass, cans and plastic bottles"
    assert council_purpose("Green Bin", MCR) == "Food and garden waste"
    assert council_purpose("Brown bin", "E09000022") is None  # only where we've been told
