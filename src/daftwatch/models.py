from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUMBER = re.compile(r"(\d[\d,]*)")
_BEDS = re.compile(r"(\d+)")
_INT = re.compile(r"(\d+)")
_RANGE = re.compile(r"from\s*€?\s*(\d[\d,]*)\s*to\s*€?\s*(\d[\d,]*)", re.IGNORECASE)


@dataclass(frozen=True)
class Listing:
    id: str
    category: str
    title: str
    url: str
    price_eur: int
    beds: int | None
    baths: int | None
    property_type: str | None
    area: str | None
    county: str | None
    lat: float | None
    lng: float | None
    raw: dict
    source: str = "daft"
    currency: str = "EUR"
    price_native: int = 0
    price_weekly: int | None = None
    first_published: str | None = None
    last_updated: str | None = None
    sharing_with: int | None = None
    rooms_available: int | None = None
    preferences: str | None = None
    owner_occupied: bool | None = None
    available_from: str | None = None
    bathroom_type: str | None = None
    description: str | None = None
    room_type: str | None = None
    city: str | None = None
    distances_km: dict[str, float] = field(default_factory=dict)
    detail_fetched: bool = False


def parse_price(text: str) -> int:
    if not text:
        return 0
    m = _NUMBER.search(text.replace(",", ""))
    if not m:
        return 0
    amount = int(m.group(1))
    if "week" in text.lower():
        return round(amount * 52 / 12)
    return amount


def parse_price_range(text: str) -> int:
    if not text:
        return 0
    m = _RANGE.search(text)
    if m:
        return int(m.group(1).replace(",", ""))
    return parse_price(text)


def parse_int(text: str | None) -> int | None:
    if text is None:
        return None
    m = _INT.search(str(text))
    return int(m.group(1)) if m else None


def parse_beds(text: str | int | None) -> int | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    m = _BEDS.search(text)
    return int(m.group(1)) if m else None
