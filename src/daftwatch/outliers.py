from __future__ import annotations

import statistics
from dataclasses import replace

from daftwatch.models import Listing

# A room priced at many times its own city's typical price is almost never a
# real room: the worst case seen was a Dublin listing at €2,100 a WEEK
# (≈€9,100 a month once converted) — most likely a whole house or a short
# let filed under "sharing". One row like that stretches every range bar and
# every axis that scales to a max, so the rest of the picture gets squashed
# into a corner.
#
# The rule is relative to the listing's own city, not an absolute cap: the
# scraper only collects "sharing" listings, so there is no separate
# whole-house rent to anchor a ceiling to, and a fixed euro figure would be
# wrong for both Dublin and Regina at once. Median (not mean), so the
# outliers themselves can't drag the yardstick up.
DEFAULT_MULTIPLIER = 3.0
# Below this many priced listings a city's median isn't a trustworthy
# yardstick — better to flag nothing than to flag off a noisy handful.
DEFAULT_MIN_SAMPLE = 15


def flag_outliers(
    listings: list[Listing],
    multiplier: float = DEFAULT_MULTIPLIER,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> list[Listing]:
    """Return *listings* with ``outlier_x`` set on every listing priced above
    ``multiplier`` × its city's median (the ratio itself, so the dashboard can
    say "3.4× the city median"). Nothing is dropped: a flagged listing stays
    in the export and stays visible — it is only excluded from the
    statistics (regression, city medians, price history), which is the caller's
    job, keyed off ``outlier_x is not None``.

    The median is taken over *available* listings with a real price, in the
    listing's own currency (a city has exactly one). Off-market listings are
    judged against that same yardstick but do not move it.
    """
    by_city: dict[str, list[int]] = {}
    for l in listings:
        if l.status == "available" and l.price_native > 0 and l.city:
            by_city.setdefault(l.city, []).append(l.price_native)
    median = {
        city: statistics.median(prices)
        for city, prices in by_city.items()
        if len(prices) >= min_sample
    }

    out: list[Listing] = []
    for l in listings:
        m = median.get(l.city) if l.city else None
        if m and l.price_native > multiplier * m:
            out.append(replace(l, outlier_x=round(l.price_native / m, 1)))
        else:
            out.append(l)
    return out

