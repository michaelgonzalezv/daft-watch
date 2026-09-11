from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

import numpy as np

from daftwatch.models import Listing

# The headline number this module exists to produce is the "country"
# coefficient: controlling for property_type and distance to the city
# centre, is Canada cheaper or pricier than Ireland?
#
# city is deliberately NOT in this regression, even though it's available
# for both sides. Every city belongs to exactly one country — Kijiji has no
# Irish cities, daft has no Canadian ones — so "country" is a deterministic
# function of "city": including full city fixed effects makes the design
# matrix rank-deficient and the country coefficient not separately
# identifiable from the city ones (numpy would still return *a* minimum-norm
# solution, but it would not mean "the country effect holding city fixed").
# City-level detail is reported separately below as plain descriptive
# medians, not as part of the regression.
_REF_COUNTRY = "Ireland"
_REF_PROPERTY_TYPE = "Room"

_CAVEATS = [
    "Controls for property_type and distance to the city centre only. "
    "Fields that exist on just one source — sharing_with, gender preference "
    "and bathroom type (Ireland only), furnished and pets allowed (Canada "
    "only) — are left out, so some of the country effect below may really "
    "be one of those, not a real market gap.",
    "property_type is a text guess for Canada (~59% coverage; the rest "
    "fall into the same reference bucket as an unclassified listing), not "
    "the structured field Ireland has.",
    "City is not a control in this model — every city belongs to exactly "
    "one country, so a country effect and a full set of city effects can't "
    "be separated statistically. See median_price_usd_by_city for city-level "
    "detail instead (plain medians, not a regression result).",
    "Cross-sectional, one snapshot in time — not a trend. Comes back with "
    "every scrape cycle, but each run only compares today's active "
    "listings, not how prices moved.",
]


def _one_hot(values: list[str], reference: str) -> tuple[list[str], list[np.ndarray]]:
    """Dummy columns for every level except *reference* (or the first level
    alphabetically, if *reference* isn't actually present). Returns the
    non-reference levels in the same order as the returned columns."""
    levels = sorted(set(values))
    ref = reference if reference in levels else levels[0]
    other_levels = [lvl for lvl in levels if lvl != ref]
    columns = [np.array([1.0 if v == lvl else 0.0 for v in values]) for lvl in other_levels]
    return other_levels, columns


def compute_comparison(listings: list[Listing], fx_usd: dict[str, float]) -> dict:
    """log(price_usd) ~ country + property_type + distance_centre_km, fit by
    plain OLS (numpy.linalg.lstsq — no external stats dependency). Returns a
    dashboard-ready dict: coefficients (with an approximate %-vs-reference
    for each dummy, exp(beta)-1 since the outcome is logged), r_squared,
    sample sizes, plain descriptive city medians, and the caveats above.
    """
    rows: list[tuple[Listing, float, float]] = []
    for l in listings:
        if l.status != "available":
            continue
        if not l.price_native or not l.currency or l.currency not in fx_usd:
            continue
        if not l.city or not l.country or not l.property_type:
            continue
        dist = l.distances_km.get("centre")
        if dist is None:
            continue
        price_usd = l.price_native * fx_usd[l.currency]
        if price_usd <= 0:
            continue
        rows.append((l, price_usd, dist))

    result: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n": len(rows),
        "caveats": _CAVEATS,
    }
    if len(rows) < 30:
        result["error"] = f"too few usable listings ({len(rows)}) to fit anything meaningful"
        return result

    countries = [l.country for l, _, _ in rows]
    ptypes = [l.property_type for l, _, _ in rows]
    dists = np.array([d for _, _, d in rows])
    prices_usd = [p for _, p, _ in rows]
    y = np.log(np.array(prices_usd))

    country_levels, country_cols = _one_hot(countries, _REF_COUNTRY)
    ptype_levels, ptype_cols = _one_hot(ptypes, _REF_PROPERTY_TYPE)

    feature_names = ["intercept"] + [f"country:{lvl}" for lvl in country_levels] + \
        [f"property_type:{lvl}" for lvl in ptype_levels] + ["distance_centre_km"]
    X = np.column_stack([np.ones(len(rows)), *country_cols, *ptype_cols, dists])

    beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    y_hat = X @ beta
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else None

    coefficients = {}
    for name, b in zip(feature_names, beta):
        entry = {"beta": round(float(b), 5)}
        if name.startswith("country:") or name.startswith("property_type:"):
            entry["pct_vs_reference"] = round((math.exp(float(b)) - 1) * 100, 1)
        elif name == "distance_centre_km":
            entry["pct_per_km"] = round((math.exp(float(b)) - 1) * 100, 1)
        coefficients[name] = entry

    sample_by_country: dict[str, int] = {}
    for c in countries:
        sample_by_country[c] = sample_by_country.get(c, 0) + 1

    by_city: dict[str, list[float]] = {}
    city_country: dict[str, str] = {}
    for l, price_usd, _ in rows:
        by_city.setdefault(l.city, []).append(price_usd)
        city_country[l.city] = l.country  # every city belongs to exactly one country
    median_by_city = {
        city: {
            "median_usd": round(statistics.median(vals)),
            "n": len(vals),
            "country": city_country[city],
        }
        for city, vals in sorted(by_city.items(), key=lambda kv: -len(kv[1]))
    }

    result.update({
        "r_squared": r_squared,
        "reference": {"country": _REF_COUNTRY, "property_type": _REF_PROPERTY_TYPE},
        "coefficients": coefficients,
        "sample_by_country": sample_by_country,
        "median_price_usd_by_city": median_by_city,
    })
    return result
