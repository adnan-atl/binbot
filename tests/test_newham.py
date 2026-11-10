"""Newham prints dates day-first ("Tuesday 10/11/2026"); upstream read them month-first."""
from datetime import date

import pytest

from app.adapters.base import NoCollectionsForAddress
from app.adapters.overrides import newham
from app.adapters.base import LookupInput

PAGE = """
<div class="card h-100"><div class="card-header">Your Address</div><p>1 Test Road</p></div>
<div class="card h-100"><div class="card-header">Your <b>Domestic</b> Collection Day --></div>
 <p class="card-text"><mark>Next</mark>&nbsp; Tuesday &nbsp;10/11/2026<br><mark>Previous</mark>&nbsp; Tuesday &nbsp;03/11/2026</p></div>
<div class="card h-100"><div class="card-header">Your <b>Recycling</b> Collection Day</div>
 <p class="card-text"><mark>Next</mark>&nbsp; Tuesday &nbsp;17/11/2026<br><mark>Previous</mark>&nbsp; Tuesday &nbsp;03/11/2026</p></div>
<div class="card h-100"><div class="card-header">Your <b>Food Waste</b> Collection Day</div>
 <p class="card-text"><mark>Next</mark>&nbsp; Tuesday &nbsp;10/11/2026<br><mark>Previous</mark>&nbsp; Tuesday &nbsp;03/11/2026</p></div>
<div class="card h-100"><div class="card-header">Green and Garden Waste (only available from Mar to Oct)</div></div>
"""
LK = LookupInput(postcode="E6 1AA", uprn="1")


def test_reads_dates_day_first_and_projects_the_round():
    got = newham(LK, html=PAGE, today=date(2026, 11, 10))
    by = {}
    for c in got:
        by.setdefault(c.label, []).append(c.date)
    assert by["Domestic waste"][:3] == [date(2026, 11, 10), date(2026, 11, 17), date(2026, 11, 24)]
    assert by["Recycling"][:2] == [date(2026, 11, 17), date(2026, 12, 1)]  # fortnightly
    assert by["Food waste"][0] == date(2026, 11, 10)
    assert "Garden" not in " ".join(by)


def test_nothing_listed_is_no_collections():
    with pytest.raises(NoCollectionsForAddress):
        newham(LK, html='<div class="card"><div class="card-header">Your Address</div></div>')
