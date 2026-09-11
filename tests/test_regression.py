from daftwatch.models import Listing
from daftwatch.regression import compute_comparison


def mk(id, country, city, price_native, currency, property_type="Room",
       distance=3.0, status="available"):
    l = Listing(
        id=id, category="sharing", title="x", url=f"https://x/{id}",
        price_eur=price_native, beds=None, baths=None,
        property_type=property_type, area=None, county=None,
        lat=1.0, lng=1.0, raw={},
        source="daft" if country == "Ireland" else "kijiji",
        currency=currency, country=country,
        price_native=price_native, city=city, status=status,
    )
    if distance is not None:
        l.distances_km["centre"] = distance  # mutating the dict, not the field
    return l


FX = {"EUR": 1.1, "CAD": 0.7}


def test_too_few_rows_returns_error_not_a_crash():
    res = compute_comparison([mk("1", "Ireland", "dublin", 800, "EUR")], FX)
    assert "error" in res
    assert res["n"] == 1
    assert "caveats" in res  # still present even on the early-return path


def test_drops_rows_missing_required_fields():
    rows = [mk(str(i), "Ireland", "dublin", 800, "EUR") for i in range(20)]
    rows += [mk(str(i), "Canada", "toronto", 700, "CAD") for i in range(20, 40)]
    # each of these is missing exactly one required field
    no_price = mk("np", "Ireland", "dublin", 0, "EUR")
    no_city = mk("nc", "Ireland", None, 800, "EUR")
    unknown_currency = mk("uc", "Ireland", "dublin", 800, "GBP")  # not in FX
    off_market = mk("om", "Ireland", "dublin", 800, "EUR", status="off_market")
    no_dist = mk("nd", "Ireland", "dublin", 800, "EUR", distance=None)
    rows += [no_price, no_city, unknown_currency, off_market, no_dist]

    res = compute_comparison(rows, FX)
    assert res["n"] == 40  # only the 40 clean rows counted


def test_country_coefficient_recovers_a_known_price_gap():
    # Ireland listings priced ~2x the (currency-converted) Canada ones, same
    # property_type and distance for everyone — the model should recover
    # something close to a 100% premium for Ireland (equivalently, Canada
    # comes out close to -50% vs the Ireland reference).
    rows = []
    for i in range(60):
        rows.append(mk(f"ie{i}", "Ireland", "dublin", 1000, "EUR", distance=2.0))
    for i in range(60):
        # 1000 EUR * 1.1 fx = 1100 USD; half of that in CAD at fx 0.7 -> /0.7
        rows.append(mk(f"ca{i}", "Canada", "toronto", round(550 / 0.7), "CAD", distance=2.0))

    res = compute_comparison(rows, FX)
    assert res["n"] == 120
    assert res["reference"]["country"] == "Ireland"
    pct = res["coefficients"]["country:Canada"]["pct_vs_reference"]
    assert -55 < pct < -45  # ~-50%, some slack for log-linear rounding


def test_property_type_reference_is_room_when_present():
    rows = [mk(f"r{i}", "Ireland", "dublin", 800, "EUR", property_type="Room") for i in range(20)]
    rows += [mk(f"h{i}", "Ireland", "dublin", 900, "EUR", property_type="House") for i in range(20)]
    res = compute_comparison(rows, FX)
    assert res["reference"]["property_type"] == "Room"
    assert "property_type:Room" not in res["coefficients"]  # reference has no column
    assert "property_type:House" in res["coefficients"]


def test_median_price_usd_by_city_is_plain_descriptive_stats():
    rows = [mk(f"a{i}", "Ireland", "dublin", 1000, "EUR") for i in range(20)]
    rows += [mk(f"b{i}", "Canada", "toronto", 700, "CAD") for i in range(20)]
    res = compute_comparison(rows, FX)
    city_stats = res["median_price_usd_by_city"]
    assert city_stats["dublin"]["n"] == 20
    assert city_stats["dublin"]["median_usd"] == round(1000 * 1.1)
    assert city_stats["dublin"]["country"] == "Ireland"
    assert city_stats["toronto"]["median_usd"] == round(700 * 0.7)
    assert city_stats["toronto"]["country"] == "Canada"


def test_no_city_fixed_effects_in_the_model():
    # city is intentionally excluded — see the module docstring on why
    # (perfectly nested within country, not separately identifiable)
    rows = [mk(f"a{i}", "Ireland", "dublin", 1000, "EUR") for i in range(20)]
    rows += [mk(f"b{i}", "Canada", "toronto", 700, "CAD") for i in range(20)]
    res = compute_comparison(rows, FX)
    assert not any(k.startswith("city:") for k in res["coefficients"])
