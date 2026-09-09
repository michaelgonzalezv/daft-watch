from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER = re.compile(r"(\d[\d,]*)")
_BEDS = re.compile(r"(\d+)")


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


def parse_beds(text: str | int | None) -> int | None:
    if text is None:
        return None
    if isinstance(text, int):
        return text
    m = _BEDS.search(text)
    return int(m.group(1)) if m else None
