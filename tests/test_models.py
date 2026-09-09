from daftwatch.models import Listing, parse_price, parse_beds


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
