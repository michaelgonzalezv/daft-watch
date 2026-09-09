from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from daftwatch.models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    category TEXT,
    title TEXT,
    url TEXT,
    price_eur INTEGER,
    beds INTEGER,
    baths INTEGER,
    property_type TEXT,
    area TEXT,
    county TEXT,
    lat REAL,
    lng REAL,
    raw_json TEXT,
    first_seen TEXT,
    last_seen TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    missing_cycles INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT,
    type TEXT,
    old_price INTEGER,
    new_price INTEGER,
    detected_at TEXT
);
CREATE TABLE IF NOT EXISTS notified (
    event_id INTEGER PRIMARY KEY,
    sent_at TEXT
);
"""


@dataclass(frozen=True)
class Event:
    id: int | None
    listing_id: str
    type: str
    old_price: int | None
    new_price: int | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._seen: set[str] = set()

    def close(self) -> None:
        self._db.close()

    def begin_cycle(self) -> None:
        self._seen = set()

    def seen_this_cycle(self) -> set[str]:
        return set(self._seen)

    def _record_event(
        self, listing_id: str, type_: str,
        old_price: int | None, new_price: int | None,
    ) -> Event:
        cur = self._db.execute(
            "INSERT INTO events (listing_id, type, old_price, new_price, detected_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (listing_id, type_, old_price, new_price, _now()),
        )
        return Event(cur.lastrowid, listing_id, type_, old_price, new_price)

    def sync(self, search_name: str, listings: list[Listing]) -> list[Event]:
        events: list[Event] = []
        now = _now()
        for l in listings:
            self._seen.add(l.id)
            row = self._db.execute(
                "SELECT price_eur, active FROM listings WHERE id = ?", (l.id,)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO listings (id, category, title, url, price_eur, beds, "
                    "baths, property_type, area, county, lat, lng, raw_json, "
                    "first_seen, last_seen, active, missing_cycles) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0)",
                    (l.id, l.category, l.title, l.url, l.price_eur, l.beds, l.baths,
                     l.property_type, l.area, l.county, l.lat, l.lng, "{}", now, now),
                )
                events.append(self._record_event(l.id, "NEW", None, l.price_eur))
                continue

            old_price = row["price_eur"]
            was_active = row["active"]
            self._db.execute(
                "UPDATE listings SET title=?, url=?, price_eur=?, beds=?, baths=?, "
                "property_type=?, area=?, county=?, lat=?, lng=?, last_seen=?, "
                "active=1, missing_cycles=0 WHERE id=?",
                (l.title, l.url, l.price_eur, l.beds, l.baths, l.property_type,
                 l.area, l.county, l.lat, l.lng, now, l.id),
            )
            if not was_active:
                events.append(self._record_event(l.id, "BACK", old_price, l.price_eur))
            elif l.price_eur and old_price and l.price_eur != old_price:
                kind = "PRICE_DROP" if l.price_eur < old_price else "PRICE_UP"
                events.append(self._record_event(l.id, kind, old_price, l.price_eur))
        self._db.commit()
        return events

    def get_listing(self, listing_id: str) -> Listing | None:
        row = self._db.execute(
            "SELECT * FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        if row is None:
            return None
        return Listing(
            id=row["id"], category=row["category"], title=row["title"],
            url=row["url"], price_eur=row["price_eur"], beds=row["beds"],
            baths=row["baths"], property_type=row["property_type"],
            area=row["area"], county=row["county"], lat=row["lat"],
            lng=row["lng"], raw={},
        )
