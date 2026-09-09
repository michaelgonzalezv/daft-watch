from __future__ import annotations
import math

CENTRES: dict[str, tuple[float, float]] = {
    "cork": (51.8979, -8.4706),
    "limerick": (52.6633, -8.6267),
    "dublin": (53.3473, -6.2591),
}


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(min(1.0, math.sqrt(a)))


def city_of(slug_or_name: str) -> str | None:
    s = (slug_or_name or "").lower()
    for key in CENTRES:
        if key in s:
            return key
    return None


def distance_to_centre(lat, lng, city):
    if lat is None or lng is None or city not in CENTRES:
        return None
    clat, clng = CENTRES[city]
    return round(haversine_km(lat, lng, clat, clng), 2)
