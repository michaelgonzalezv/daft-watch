from __future__ import annotations

import abc

from daftwatch.config import Search
from daftwatch.models import Listing, parse_beds, parse_price


class AdapterError(Exception):
    pass


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_listing(d: dict, category: str) -> Listing:
    return Listing(
        id=str(d.get("id", "")),
        category=category,
        title=d.get("title") or "",
        url=d.get("daft_link") or d.get("url") or "",
        price_eur=parse_price(d.get("price") or ""),
        beds=parse_beds(d.get("bedrooms")),
        baths=parse_beds(d.get("bathrooms")),
        property_type=d.get("category"),
        area=d.get("location") or d.get("area"),
        county=d.get("county"),
        lat=_f(d.get("latitude")),
        lng=_f(d.get("longitude")),
        raw=d,
    )


class SearchAdapter(abc.ABC):
    @abc.abstractmethod
    def fetch(self, search: Search) -> list[Listing]:
        ...
