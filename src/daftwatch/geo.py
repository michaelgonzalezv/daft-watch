from __future__ import annotations
import math

CENTRES: dict[str, tuple[float, float]] = {
    "cork": (51.8979, -8.4706),
    "limerick": (52.6633, -8.6267),
    "dublin": (53.3473, -6.2591),
    # Canada (Kijiji) — one fixed centre point per search region.
    "toronto": (43.6532, -79.3832),
    "mississauga": (43.5890, -79.6441),
    "montreal": (45.5019, -73.5674),
    "ottawa": (45.4215, -75.6972),
    "calgary": (51.0447, -114.0719),
    "hamilton": (43.2557, -79.8711),
    "kitchener": (43.4516, -80.4925),
    "edmonton": (53.5461, -113.4938),
    "london": (42.9849, -81.2453),
    "vancouver": (49.2827, -123.1207),
    "winnipeg": (49.8951, -97.1384),
    "catharines": (43.1594, -79.2469),   # St. Catharines
    "halifax": (44.6488, -63.5752),
    "quebec": (46.8139, -71.2080),       # Quebec City
    "saskatoon": (52.1332, -106.6700),
    "regina": (50.4452, -104.6189),
    "kelowna": (49.8880, -119.4960),
    "victoria": (48.4284, -123.3656),
    "windsor": (42.3149, -83.0364),
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
