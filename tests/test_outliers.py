from daftwatch.models import Listing
from daftwatch.outliers import flag_outliers


def mk(id, city, price_native, status="available", currency="EUR"):
    return Listing(
        id=id, category="sharing", title="x", url=f"https://x/{id}",
        price_eur=price_native, beds=None, baths=None, property_type="Room",
        area=None, county=None, lat=1.0, lng=1.0, raw={},
        currency=currency, price_native=price_native, city=city, status=status,
    )


def city_of(n, city, price):
    return [mk(f"{city}{i}", city, price) for i in range(n)]


def test_flags_a_price_far_above_the_city_median_with_its_ratio():
    rows = city_of(20, "dublin", 900) + [mk("weekly", "dublin", 9100)]
    out = {l.id: l for l in flag_outliers(rows)}
    assert out["weekly"].outlier_x == round(9100 / 900, 1)  # 10.1
    assert out["dublin0"].outlier_x is None


def test_a_price_just_under_the_multiplier_is_left_alone():
    rows = city_of(20, "dublin", 900) + [mk("high", "dublin", 2600)]  # 2.9x < 3x
    assert {l.id: l for l in flag_outliers(rows)}["high"].outlier_x is None


def test_multiplier_is_configurable():
    rows = city_of(20, "dublin", 900) + [mk("high", "dublin", 2000)]
    assert {l.id: l for l in flag_outliers(rows, multiplier=2.0)}["high"].outlier_x == 2.2


def test_nothing_is_dropped_only_marked():
    rows = city_of(20, "dublin", 900) + [mk("weekly", "dublin", 9100)]
    assert [l.id for l in flag_outliers(rows)] == [l.id for l in rows]


def test_a_thin_city_is_never_flagged():
    # median of 5 listings isn't a yardstick worth trusting
    rows = city_of(5, "regina", 500) + [mk("weird", "regina", 5000)]
    assert all(l.outlier_x is None for l in flag_outliers(rows))


def test_the_outlier_does_not_drag_its_own_yardstick_up():
    # mean would be ~1.2k here; the median stays 900, so 9100 is still caught
    rows = city_of(20, "dublin", 900) + [mk(f"big{i}", "dublin", 9100) for i in range(3)]
    assert sum(1 for l in flag_outliers(rows) if l.outlier_x is not None) == 3


def test_each_city_is_judged_against_its_own_median():
    rows = city_of(20, "dublin", 900) + city_of(20, "regina", 450) + [mk("r", "regina", 1400)]
    out = {l.id: l for l in flag_outliers(rows)}
    assert out["r"].outlier_x == round(1400 / 450, 1)  # 3.1x regina, though only 1.6x dublin


def test_off_market_listings_are_judged_but_dont_move_the_median():
    rows = city_of(20, "dublin", 900) + [mk("gone", "dublin", 9100, status="off_market")]
    assert {l.id: l for l in flag_outliers(rows)}["gone"].outlier_x == round(9100 / 900, 1)


def test_unpriced_and_cityless_listings_are_ignored():
    rows = city_of(20, "dublin", 900) + [mk("free", "dublin", 0), mk("nowhere", None, 99999)]
    out = {l.id: l for l in flag_outliers(rows)}
    assert out["free"].outlier_x is None and out["nowhere"].outlier_x is None
