import pytest

from daftwatch.models import (
    Listing,
    parse_price,
    parse_beds,
    parse_price_range,
    parse_int,
)


def _minimal_listing():
    return Listing(
        id="1", category="rent", title="t", url="u", price_eur=1000,
        beds=1, baths=1, property_type="Apartment", area="D8",
        county="Dublin", lat=None, lng=None, raw={},
    )


def test_new_fields_have_documented_defaults():
    l = _minimal_listing()
    assert l.source == "daft"
    assert l.currency == "EUR"
    assert l.price_native == 0
    assert l.price_weekly is None
    assert l.first_published is None
    assert l.last_updated is None
    assert l.sharing_with is None
    assert l.rooms_available is None
    assert l.preferences is None
    assert l.owner_occupied is None
    assert l.available_from is None
    assert l.bathroom_type is None
    assert l.description is None
    assert l.room_type is None
    assert l.city is None
    assert l.distances_km == {}
    assert l.detail_fetched is False


def test_distances_km_default_not_shared():
    a = _minimal_listing()
    b = _minimal_listing()
    assert a.distances_km is not b.distances_km


def test_new_fields_still_frozen():
    l = _minimal_listing()
    with pytest.raises(Exception):
        l.city = "dublin"


def test_parse_price_range_from_to():
    assert parse_price_range("From €725 to €750 per month") == 725


def test_parse_price_range_weekly():
    assert parse_price_range("€160 per week") == round(160 * 52 / 12)


def test_parse_price_range_weekly_range_normalized():
    assert parse_price_range("From €150 to €175 per week") == round(150 * 52 / 12)


def test_parse_price_range_delegates_plain():
    assert parse_price_range("€1750") == 1750


def test_parse_price_range_delegates_on_application():
    assert parse_price_range("Price on Application") == 0


def test_parse_price_range_delegates_empty():
    assert parse_price_range("") == 0


def test_parse_int_finds_first_integer():
    assert parse_int("Sharing with 3 people") == 3


def test_parse_int_none_for_no_digits():
    assert parse_int("n/a") is None


def test_parse_int_none_for_none():
    assert parse_int(None) is None


def test_parse_price_monthly():
    assert parse_price("€2,200 per month") == 2200


def test_parse_price_weekly_converts_to_monthly():
    assert parse_price("€500 per week") == round(500 * 52 / 12)


def test_parse_price_plain_number():
    assert parse_price("€1750") == 1750


def test_parse_price_on_application():
    assert parse_price("Price on application") == 0


def test_parse_price_empty():
    assert parse_price("") == 0


def test_parse_beds_from_string():
    assert parse_beds("2 Bed") == 2


def test_parse_beds_from_int():
    assert parse_beds(3) == 3


def test_parse_beds_from_float():
    assert parse_beds(2.0) == 2


def test_parse_beds_none():
    assert parse_beds(None) is None
    assert parse_beds("Studio") is None


def test_listing_is_frozen():
    l = Listing(
        id="1", category="rent", title="t", url="u", price_eur=1000,
        beds=1, baths=1, property_type="Apartment", area="D8",
        county="Dublin", lat=None, lng=None, raw={},
    )
    try:
        l.id = "2"
    except Exception as e:
        assert "frozen" in str(type(e)).lower() or "cannot assign" in str(e).lower()
    else:
        raise AssertionError("Listing should be frozen")
