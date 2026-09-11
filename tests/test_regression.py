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
    assert city_stats["dublin"]["min_usd"] == round(1000 * 1.1)
    assert city_stats["dublin"]["max_usd"] == round(1000 * 1.1)
    assert city_stats["dublin"]["country"] == "Ireland"
    assert city_stats["toronto"]["median_usd"] == round(700 * 0.7)
    assert city_stats["toronto"]["country"] == "Canada"


def test_median_price_usd_by_city_min_max_span_varying_prices():
    prices = [600, 900, 1200] * 10  # 30 rows total — at the n>=30 floor
    rows = [mk(f"a{i}", "Ireland", "dublin", p, "EUR") for i, p in enumerate(prices)]
    res = compute_comparison(rows, FX)
    stats = res["median_price_usd_by_city"]["dublin"]
    assert stats["min_usd"] == round(600 * 1.1)
    assert stats["max_usd"] == round(1200 * 1.1)
    assert stats["median_usd"] == round(900 * 1.1)


def test_median_price_usd_by_city_also_reports_native_currency_figures():
    # a EUR price against Ireland's EUR/hr minimum wage (or CAD against
    # Canada's) needs no fx at all — these must be the untouched native
    # prices, not the USD figures divided back through fx_usd.
    rows = [mk(f"a{i}", "Ireland", "dublin", 1000, "EUR") for i in range(20)]
    rows += [mk(f"b{i}", "Canada", "toronto", 700, "CAD") for i in range(20)]
    res = compute_comparison(rows, FX)
    dublin = res["median_price_usd_by_city"]["dublin"]
    toronto = res["median_price_usd_by_city"]["toronto"]
    assert dublin["median_native"] == 1000
    assert dublin["min_native"] == 1000
    assert dublin["max_native"] == 1000
    assert dublin["currency"] == "EUR"
    assert toronto["median_native"] == 700
    assert toronto["currency"] == "CAD"


def test_median_price_usd_by_city_native_min_max_span_varying_prices():
    prices = [600, 900, 1200] * 10
    rows = [mk(f"a{i}", "Ireland", "dublin", p, "EUR") for i, p in enumerate(prices)]
    res = compute_comparison(rows, FX)
    stats = res["median_price_usd_by_city"]["dublin"]
    assert stats["min_native"] == 600
    assert stats["max_native"] == 1200
    assert stats["median_native"] == 900


def test_no_city_fixed_effects_in_the_model():
    # city is intentionally excluded — see the module docstring on why
    # (perfectly nested within country, not separately identifiable)
    rows = [mk(f"a{i}", "Ireland", "dublin", 1000, "EUR") for i in range(20)]
    rows += [mk(f"b{i}", "Canada", "toronto", 700, "CAD") for i in range(20)]
    res = compute_comparison(rows, FX)
    assert not any(k.startswith("city:") for k in res["coefficients"])


def test_property_type_by_country_fits_each_country_separately():
    # Ireland: House costs noticeably more than Room. Canada: no real gap.
    # A per-country fit should recover each pattern on its own, distinct from
    # the pooled coefficients (which mix both countries together).
    rows = [mk(f"ie_room{i}", "Ireland", "dublin", 800, "EUR", property_type="Room") for i in range(20)]
    rows += [mk(f"ie_house{i}", "Ireland", "dublin", 1200, "EUR", property_type="House") for i in range(20)]
    rows += [mk(f"ca_room{i}", "Canada", "toronto", 700, "CAD", property_type="Room") for i in range(20)]
    rows += [mk(f"ca_house{i}", "Canada", "toronto", 700, "CAD", property_type="House") for i in range(20)]

    res = compute_comparison(rows, FX)
    by_country = res["property_type_by_country"]

    ie_house_pct = by_country["Ireland"]["coefficients"]["property_type:House"]["pct_vs_reference"]
    ca_house_pct = by_country["Canada"]["coefficients"]["property_type:House"]["pct_vs_reference"]
    assert ie_house_pct > 40  # 1200 vs 800 -> +50%, roughly
    assert -1 < ca_house_pct < 1  # 700 vs 700 -> no gap
    assert by_country["Ireland"]["n"] == 40
    assert by_country["Canada"]["n"] == 40


def test_per_country_fit_reports_the_reference_it_actually_used():
    # The real shape of the data: Ireland has NO "Room" listing at all (daft's
    # property_type is Apartment / House / Private Rental Sector), so its fit
    # cannot be referenced to Room and silently falls back to the first level
    # alphabetically. The fit has to say so — the dashboard prints "vs <ref>",
    # and printing "vs Room" over Apartment-referenced numbers is a wrong
    # number, not a wrong word.
    rows = [mk(f"ie_apt{i}", "Ireland", "dublin", 1000, "EUR", property_type="Apartment") for i in range(20)]
    rows += [mk(f"ie_house{i}", "Ireland", "dublin", 1200, "EUR", property_type="House") for i in range(20)]
    rows += [mk(f"ca_room{i}", "Canada", "toronto", 700, "CAD", property_type="Room") for i in range(20)]
    rows += [mk(f"ca_house{i}", "Canada", "toronto", 900, "CAD", property_type="House") for i in range(20)]

    res = compute_comparison(rows, FX)
    by_country = res["property_type_by_country"]

    assert by_country["Ireland"]["reference"] == "Apartment"  # no Room in Ireland
    assert by_country["Canada"]["reference"] == "Room"
    # and the percentages really are against that reference: 1200 vs 1000
    ie = by_country["Ireland"]["coefficients"]["property_type:House"]["pct_vs_reference"]
    assert 15 < ie < 25  # ~+20% vs Apartment
    # the reference level itself never gets a column
    assert "property_type:Apartment" not in by_country["Ireland"]["coefficients"]
    assert "property_type:Room" not in by_country["Canada"]["coefficients"]


def test_pooled_reference_reports_the_level_actually_used_not_the_constant():
    # Nothing here is a "Room", so the pooled fit falls back to Apartment and
    # must report that rather than the requested _REF_PROPERTY_TYPE.
    rows = [mk(f"a{i}", "Ireland", "dublin", 1000, "EUR", property_type="Apartment") for i in range(20)]
    rows += [mk(f"b{i}", "Canada", "toronto", 700, "CAD", property_type="House") for i in range(20)]
    res = compute_comparison(rows, FX)
    assert res["reference"]["property_type"] == "Apartment"
    assert res["reference"]["country"] == "Ireland"


def test_property_type_by_country_omits_a_too_thin_country():
    rows = [mk(f"ie{i}", "Ireland", "dublin", 800, "EUR", property_type="Room") for i in range(15)]
    rows += [mk(f"ie2_{i}", "Ireland", "dublin", 1200, "EUR", property_type="House") for i in range(15)]
    rows += [mk(f"ca{i}", "Canada", "toronto", 700, "CAD") for i in range(5)]  # under the n>=30 floor
    res = compute_comparison(rows, FX)
    assert "Ireland" in res["property_type_by_country"]
    assert "Canada" not in res["property_type_by_country"]
