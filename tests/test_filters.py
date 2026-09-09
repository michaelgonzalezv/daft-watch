from daftwatch.models import Listing
from daftwatch.filters import apply, within_distance


def mk(id, title, price_eur=1000, sharing_with=None, city=None, distances_km=None):
    return Listing(
        id=id, category="rent", title=title, url="u", price_eur=price_eur,
        beds=1, baths=1, property_type=None, area=None, county=None,
        lat=None, lng=None, raw={}, sharing_with=sharing_with, city=city,
        distances_km=distances_km or {}
    )


def test_empty_config_passes_everything():
    ls = [mk("1", "Nice flat"), mk("2", "Student digs")]
    assert apply(ls, {}) == ls


def test_empty_config_returns_copy():
    """apply({}) returns a copy, not the same list."""
    ls = [mk("1", "Nice flat"), mk("2", "Student digs")]
    out = apply(ls, {})
    assert out is not ls
    assert out == ls


def test_keywords_exclude_case_insensitive():
    ls = [mk("1", "Nice flat"), mk("2", "STUDENT accommodation"),
          mk("3", "short term let")]
    out = apply(ls, {"keywords_exclude": ["student", "short term"]})
    assert [l.id for l in out] == ["1"]


def test_unknown_keys_ignored():
    ls = [mk("1", "flat")]
    assert apply(ls, {"nonsense": True}) == ls


# Tests for max_price filter
def test_max_price_drops_expensive():
    ls = [mk("1", "cheap", price_eur=750),
          mk("2", "medium", price_eur=800),
          mk("3", "expensive", price_eur=850)]
    out = apply(ls, {"max_price": 800})
    assert [l.id for l in out] == ["1", "2"]


def test_max_price_keeps_equal():
    ls = [mk("1", "at_cap", price_eur=800)]
    out = apply(ls, {"max_price": 800})
    assert [l.id for l in out] == ["1"]


def test_max_price_none_passes_all():
    ls = [mk("1", "expensive", price_eur=900)]
    out = apply(ls, {})  # no max_price key
    assert [l.id for l in out] == ["1"]


# Tests for max_sharing_with filter
def test_max_sharing_with_drops_high():
    ls = [mk("1", "alone", sharing_with=None),
          mk("2", "quad", sharing_with=4),
          mk("3", "quintet", sharing_with=5)]
    out = apply(ls, {"max_sharing_with": 4})
    assert [l.id for l in out] == ["1", "2"]


def test_max_sharing_with_keeps_unknown():
    """Listings with sharing_with=None (unknown) are kept."""
    ls = [mk("1", "unknown", sharing_with=None),
          mk("2", "high", sharing_with=10)]
    out = apply(ls, {"max_sharing_with": 5})
    assert [l.id for l in out] == ["1"]


def test_max_sharing_with_keeps_equal():
    ls = [mk("1", "at_cap", sharing_with=4)]
    out = apply(ls, {"max_sharing_with": 4})
    assert [l.id for l in out] == ["1"]


# Combined filter tests
def test_combined_filters():
    """Combining max_price, keywords_exclude, and max_sharing_with."""
    ls = [
        mk("1", "Nice cheap quad", price_eur=700, sharing_with=4),
        mk("2", "Student digs expensive", price_eur=750, sharing_with=3),
        mk("3", "Nice expensive solo", price_eur=850, sharing_with=None),
        mk("4", "Cheap shared many", price_eur=600, sharing_with=6),
    ]
    out = apply(ls, {
        "keywords_exclude": ["student"],
        "max_price": 800,
        "max_sharing_with": 4,
    })
    # Exclude "Student" → drop id 2
    # max_price 800 → drop id 3 (850)
    # max_sharing_with 4 → drop id 4 (6), but keep id 1 (4)
    # Result: [1]
    assert [l.id for l in out] == ["1"]


# Tests for within_distance function
def test_within_distance_keeps_unknown_city():
    """Keep listing when city is None."""
    ls = [mk("1", "unknown", city=None, distances_km={"centre": 5.0})]
    out = within_distance(ls, {"dublin": 10.0})
    assert [l.id for l in out] == ["1"]


def test_within_distance_keeps_unlisted_city():
    """Keep listing when its city is not in limits."""
    ls = [mk("1", "galway", city="galway", distances_km={"centre": 8.0})]
    out = within_distance(ls, {"dublin": 6.0})
    assert [l.id for l in out] == ["1"]


def test_within_distance_keeps_unknown_distance():
    """Keep listing when distances_km has no 'centre' key."""
    ls = [mk("1", "dublin", city="dublin", distances_km={})]
    out = within_distance(ls, {"dublin": 6.0})
    assert [l.id for l in out] == ["1"]


def test_within_distance_keeps_close():
    """Keep listing when centre distance is within limit."""
    ls = [mk("1", "dublin_close", city="dublin", distances_km={"centre": 3.0})]
    out = within_distance(ls, {"dublin": 6.0})
    assert [l.id for l in out] == ["1"]


def test_within_distance_drops_far():
    """Drop listing when centre distance exceeds limit."""
    ls = [mk("1", "dublin_far", city="dublin", distances_km={"centre": 8.0})]
    out = within_distance(ls, {"dublin": 6.0})
    assert [l.id for l in out] == []


def test_within_distance_edge_equal():
    """Keep listing when centre distance equals limit."""
    ls = [mk("1", "dublin_edge", city="dublin", distances_km={"centre": 6.0})]
    out = within_distance(ls, {"dublin": 6.0})
    assert [l.id for l in out] == ["1"]


def test_within_distance_multiple_listings():
    """Multiple listings with different cities and distances."""
    ls = [
        mk("1", "dublin_close", city="dublin", distances_km={"centre": 3.0}),
        mk("2", "dublin_far", city="dublin", distances_km={"centre": 8.0}),
        mk("3", "cork", city="cork", distances_km={"centre": 5.0}),
        mk("4", "limerick", city="limerick", distances_km={"centre": 2.0}),
        mk("5", "no_city", city=None, distances_km={"centre": 100.0}),
    ]
    limits = {"dublin": 6.0, "limerick": 5.0}
    out = within_distance(ls, limits)
    # 1: dublin, 3.0 <= 6.0 ✓
    # 2: dublin, 8.0 > 6.0 ✗
    # 3: cork not in limits ✓
    # 4: limerick, 2.0 <= 5.0 ✓
    # 5: city=None ✓
    assert [l.id for l in out] == ["1", "3", "4", "5"]
