import json
from pathlib import Path

import pytest
from daftwatch.adapter import SearchAdapter, AdapterError, to_listing

FIX = Path(__file__).parent / "fixtures"


def test_to_listing_monthly():
    d = json.loads((FIX / "daft_rent_page1.json").read_text())[0]
    l = to_listing(d, "rent")
    assert l.id == "5001"
    assert l.category == "rent"
    assert l.price_eur == 2100
    assert l.beds == 2
    assert l.property_type == "Apartment"
    assert l.url.endswith("/5001")
    assert l.lat == 53.3331


def test_to_listing_weekly_converts():
    d = json.loads((FIX / "daft_rent_page1.json").read_text())[1]
    l = to_listing(d, "rent")
    assert l.price_eur == round(400 * 52 / 12)
    assert l.beds is None


def test_to_listing_tolerates_missing_keys():
    l = to_listing({"id": "9"}, "sharing")
    assert l.id == "9"
    assert l.price_eur == 0
    assert l.beds is None
    assert l.url == ""


def test_searchadapter_is_abstract():
    with pytest.raises(TypeError):
        SearchAdapter()
