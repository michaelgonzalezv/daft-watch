from __future__ import annotations

from daftwatch.models import Listing


def apply(listings: list[Listing], filter_config: dict) -> list[Listing]:
    """
    Apply filters to listings based on filter_config.

    Supported keys:
    - keywords_exclude (list[str]): exclude listings with these words in title
    - max_price (int): drop listings where price_eur > max_price
    - max_sharing_with (int): drop listings where sharing_with > max_sharing_with
                              (but keep listings with sharing_with=None)

    Returns: filtered copy of listings, preserving order.
    """
    config = filter_config or {}

    # Start with all listings
    out = list(listings)

    # Apply keywords_exclude
    excludes = [w.lower() for w in config.get("keywords_exclude", [])]
    if excludes:
        filtered = []
        for l in out:
            title = (l.title or "").lower()
            if not any(word in title for word in excludes):
                filtered.append(l)
        out = filtered

    # Apply max_price
    if "max_price" in config:
        max_price = config["max_price"]
        out = [l for l in out if l.price_eur <= max_price]

    # Apply max_sharing_with (keep None values, only filter known values)
    if "max_sharing_with" in config:
        max_sharing = config["max_sharing_with"]
        out = [l for l in out if l.sharing_with is None or l.sharing_with <= max_sharing]

    return out


def within_distance(listings: list[Listing], limits: dict[str, float]) -> list[Listing]:
    """
    Filter listings by distance from city centres.

    Keep a listing when:
    - listing.city is None (unknown city), OR
    - listing.city is not in limits (no limit for this city), OR
    - distances_km["centre"] is missing/None (unknown distance), OR
    - distances_km["centre"] <= limits[city]

    Args:
        listings: list of Listing objects
        limits: dict mapping city names to max distance in km (e.g. {"dublin": 6.0})

    Returns: filtered list, preserving order
    """
    out = []
    for l in listings:
        # Keep if city is unknown
        if l.city is None:
            out.append(l)
            continue

        # Keep if city has no limit
        if l.city not in limits:
            out.append(l)
            continue

        # Keep if distance is unknown
        centre_dist = l.distances_km.get("centre")
        if centre_dist is None:
            out.append(l)
            continue

        # Keep if within limit
        if centre_dist <= limits[l.city]:
            out.append(l)

    return out
