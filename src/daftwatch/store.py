from __future__ import annotations

import json
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
    missing_cycles INTEGER NOT NULL DEFAULT 0,
    source TEXT DEFAULT 'daft',
    currency TEXT DEFAULT 'EUR',
    price_native INTEGER DEFAULT 0,
    price_weekly INTEGER,
    first_published TEXT,
    last_updated TEXT,
    sharing_with INTEGER,
    rooms_available INTEGER,
    preferences TEXT,
    owner_occupied INTEGER,
    available_from TEXT,
    bathroom_type TEXT,
    description TEXT,
    room_type TEXT,
    city TEXT,
    detail_json TEXT,
    detail_fetched INTEGER NOT NULL DEFAULT 0
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
PRAGMA user_version = 2;
"""

# New v2 columns, added to an existing v1 DB by ``_migrate``. Keep in sync with
# the ``CREATE TABLE listings`` block above.
_V2_COLUMNS = [
    ("source", "TEXT DEFAULT 'daft'"),
    ("currency", "TEXT DEFAULT 'EUR'"),
    ("price_native", "INTEGER DEFAULT 0"),
    ("price_weekly", "INTEGER"),
    ("first_published", "TEXT"),
    ("last_updated", "TEXT"),
    ("sharing_with", "INTEGER"),
    ("rooms_available", "INTEGER"),
    ("preferences", "TEXT"),
    ("owner_occupied", "INTEGER"),
    ("available_from", "TEXT"),
    ("bathroom_type", "TEXT"),
    ("description", "TEXT"),
    ("room_type", "TEXT"),
    ("city", "TEXT"),
    ("detail_json", "TEXT"),
    ("detail_fetched", "INTEGER NOT NULL DEFAULT 0"),
]


@dataclass(frozen=True)
class Event:
    id: int | None
    listing_id: str
    type: str
    old_price: int | None
    new_price: int | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bit(value: bool | None) -> int | None:
    """bool -> 0/1, None -> None (for nullable 3-state INTEGER columns)."""
    if value is None:
        return None
    return int(bool(value))


def _migrate(db: sqlite3.Connection) -> None:
    """Bring an existing DB up to schema v2.

    Runs before ``executescript(SCHEMA)`` so the version check is meaningful:
    a fresh DB has no ``listings`` table yet and is left to ``SCHEMA``; a v0/v1
    or partially-migrated DB gets each missing column added and the version
    stamped.
    """
    has_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='listings'"
    ).fetchone()
    if not has_table:
        return
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version >= 2:
        return
    existing = {r[1] for r in db.execute("PRAGMA table_info(listings)")}
    for name, decl in _V2_COLUMNS:
        if name not in existing:
            db.execute(f"ALTER TABLE listings ADD COLUMN {name} {decl}")
    db.execute("PRAGMA user_version = 2")
    db.commit()


def _row_to_listing(row: sqlite3.Row) -> Listing:
    distances: dict[str, float] = {}
    raw_detail = row["detail_json"]
    if raw_detail:
        try:
            parsed = json.loads(raw_detail)
            if isinstance(parsed, dict) and isinstance(parsed.get("distances_km"), dict):
                distances = parsed["distances_km"]
        except (ValueError, TypeError):
            distances = {}
    owner = row["owner_occupied"]
    return Listing(
        id=row["id"], category=row["category"], title=row["title"],
        url=row["url"], price_eur=row["price_eur"], beds=row["beds"],
        baths=row["baths"], property_type=row["property_type"],
        area=row["area"], county=row["county"], lat=row["lat"], lng=row["lng"],
        raw={},
        source=row["source"] or "daft",
        currency=row["currency"] or "EUR",
        price_native=row["price_native"] or 0,
        price_weekly=row["price_weekly"],
        first_published=row["first_published"],
        last_updated=row["last_updated"],
        sharing_with=row["sharing_with"],
        rooms_available=row["rooms_available"],
        preferences=row["preferences"],
        owner_occupied=None if owner is None else bool(owner),
        available_from=row["available_from"],
        bathroom_type=row["bathroom_type"],
        description=row["description"],
        room_type=row["room_type"],
        city=row["city"],
        distances_km=distances,
        detail_fetched=bool(row["detail_fetched"]),
    )


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        _migrate(self._db)
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
            detail_json = json.dumps({"distances_km": l.distances_km})
            if row is None:
                self._db.execute(
                    "INSERT INTO listings (id, category, title, url, price_eur, beds, "
                    "baths, property_type, area, county, lat, lng, raw_json, "
                    "first_seen, last_seen, active, missing_cycles, "
                    "source, currency, price_native, price_weekly, first_published, "
                    "last_updated, sharing_with, rooms_available, preferences, "
                    "owner_occupied, available_from, bathroom_type, description, "
                    "room_type, city, detail_json, detail_fetched) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,"
                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (l.id, l.category, l.title, l.url, l.price_eur, l.beds, l.baths,
                     l.property_type, l.area, l.county, l.lat, l.lng,
                     json.dumps(l.raw, default=str), now, now,
                     l.source, l.currency, l.price_native, l.price_weekly,
                     l.first_published, l.last_updated, l.sharing_with,
                     l.rooms_available, l.preferences, _bit(l.owner_occupied),
                     l.available_from, l.bathroom_type, l.description,
                     l.room_type, l.city, detail_json, int(bool(l.detail_fetched))),
                )
                events.append(self._record_event(l.id, "NEW", None, l.price_eur))
                continue

            old_price = row["price_eur"]
            was_active = row["active"]
            # Only search-page-derived fields are refreshed here; detail columns
            # (sharing_with, ...), detail_json, detail_fetched and city are owned
            # by apply_detail / set_distances / set_city and must survive a
            # re-sync.
            self._db.execute(
                "UPDATE listings SET title=?, url=?, price_eur=?, beds=?, baths=?, "
                "property_type=?, area=?, county=?, lat=?, lng=?, raw_json=?, "
                "last_seen=?, active=1, missing_cycles=0, "
                "source=?, currency=?, price_native=?, price_weekly=?, "
                "first_published=?, room_type=? WHERE id=?",
                (l.title, l.url, l.price_eur, l.beds, l.baths, l.property_type,
                 l.area, l.county, l.lat, l.lng, json.dumps(l.raw, default=str),
                 now, l.source, l.currency, l.price_native, l.price_weekly,
                 l.first_published, l.room_type, l.id),
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
        return _row_to_listing(row)

    def active_listings(self) -> list[Listing]:
        rows = self._db.execute(
            "SELECT * FROM listings WHERE active = 1 ORDER BY first_seen ASC, id ASC"
        ).fetchall()
        return [_row_to_listing(r) for r in rows]

    def needs_detail(self, price_cap: int, limit: int | None = None) -> list[str]:
        sql = (
            "SELECT id FROM listings WHERE active = 1 AND detail_fetched = 0 "
            "AND price_eur > 0 AND price_eur <= ? ORDER BY first_seen ASC, id ASC"
        )
        params: list[object] = [price_cap]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [r[0] for r in self._db.execute(sql, params).fetchall()]

    def apply_detail(self, listing_id: str, fields: dict) -> None:
        self._db.execute(
            "UPDATE listings SET sharing_with=?, rooms_available=?, preferences=?, "
            "owner_occupied=?, available_from=?, bathroom_type=?, description=?, "
            "last_updated=?, detail_fetched=1 WHERE id=?",
            (
                fields.get("sharing_with"),
                fields.get("rooms_available"),
                fields.get("preferences"),
                _bit(fields.get("owner_occupied")),
                fields.get("available_from"),
                fields.get("bathroom_type"),
                fields.get("description"),
                fields.get("last_updated"),
                listing_id,
            ),
        )
        self._db.commit()

    def _set_distances_nocommit(
        self, listing_id: str, distances: dict[str, float]
    ) -> None:
        row = self._db.execute(
            "SELECT detail_json FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        if row is None:
            return
        data: dict = {}
        if row["detail_json"]:
            try:
                loaded = json.loads(row["detail_json"])
                if isinstance(loaded, dict):
                    data = loaded
            except (ValueError, TypeError):
                data = {}
        merged = dict(data.get("distances_km") or {})
        merged.update(distances)
        data["distances_km"] = merged
        self._db.execute(
            "UPDATE listings SET detail_json = ? WHERE id = ?",
            (json.dumps(data), listing_id),
        )

    def set_distances(self, listing_id: str, distances: dict[str, float]) -> None:
        self._set_distances_nocommit(listing_id, distances)
        self._db.commit()

    def _set_city_nocommit(self, listing_id: str, city: str | None) -> None:
        self._db.execute(
            "UPDATE listings SET city = ? WHERE id = ?", (city, listing_id)
        )

    def set_city(self, listing_id: str, city: str | None) -> None:
        self._set_city_nocommit(listing_id, city)
        self._db.commit()

    def set_cities_and_distances(
        self, mapping: dict[str, tuple[str | None, float | None]]
    ) -> None:
        """Persist ``{listing_id: (city, centre_distance_km)}`` in one commit.

        A ``None`` distance updates only the city (no centre distance known).
        """
        for listing_id, (city, dist) in mapping.items():
            self._set_city_nocommit(listing_id, city)
            if dist is not None:
                self._set_distances_nocommit(listing_id, {"centre": dist})
        self._db.commit()

    def finish_cycle(self, gone_after_cycles: int) -> list[Event]:
        events: list[Event] = []
        rows = self._db.execute(
            "SELECT id, price_eur, missing_cycles FROM listings WHERE active = 1"
        ).fetchall()
        for row in rows:
            if row["id"] in self._seen:
                continue
            new_missing = row["missing_cycles"] + 1
            if new_missing >= gone_after_cycles:
                self._db.execute(
                    "UPDATE listings SET active = 0, missing_cycles = ? WHERE id = ?",
                    (new_missing, row["id"]),
                )
                events.append(
                    self._record_event(row["id"], "GONE", row["price_eur"], None)
                )
            else:
                self._db.execute(
                    "UPDATE listings SET missing_cycles = ? WHERE id = ?",
                    (new_missing, row["id"]),
                )
        self._db.commit()
        return events

    def pending_events(self) -> list[Event]:
        rows = self._db.execute(
            "SELECT e.id, e.listing_id, e.type, e.old_price, e.new_price "
            "FROM events e LEFT JOIN notified n ON n.event_id = e.id "
            "WHERE n.event_id IS NULL ORDER BY e.id"
        ).fetchall()
        return [
            Event(r["id"], r["listing_id"], r["type"], r["old_price"], r["new_price"])
            for r in rows
        ]

    def mark_notified(self, event_ids: list[int]) -> None:
        now = _now()
        self._db.executemany(
            "INSERT OR IGNORE INTO notified (event_id, sent_at) VALUES (?, ?)",
            [(eid, now) for eid in event_ids],
        )
        self._db.commit()
