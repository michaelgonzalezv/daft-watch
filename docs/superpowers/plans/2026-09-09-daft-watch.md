# daft-watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A personal Docker service that polls daft.ie rental and house-share searches on an interval, stores listings with history in SQLite, and emails the user when a listing is new, drops in price, or disappears.

**Architecture:** A Python package `daftwatch`. Fetching goes through a `SearchAdapter` interface; the only implementation wraps the `daftlistings` library so a library break is contained to one class. Each cycle: fetch every configured search, upsert snapshots into SQLite and compute diff events, filter them, send one digest email, and record which events were notified so a failed send retries without duplicating. A scheduling loop lives inside the container.

**Tech Stack:** Python 3.12, `daftlistings` (pinned), `PyYAML`, standard-library `sqlite3` / `smtplib` / `email`, `pytest`, Docker + docker-compose.

**Spec:** `docs/superpowers/specs/2026-09-09-daft-watch-design.md`

## Global Constraints

- Python 3.12; run as non-root in the container.
- Only two listing categories: `rent` and `sharing`. No sale/commercial/land.
- Credentials (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `ALERT_FROM`, `ALERT_TO`) come from environment variables only — never from `config.yaml`, never committed.
- All prices stored and compared as integer euros per month. Weekly prices convert with `round(amount * 52 / 12)`.
- Dependencies limited to: `daftlistings`, `PyYAML`, `pytest` (dev). Everything else must be standard library.
- `daftlistings` version is pinned in `requirements.txt` with `==`.
- HTTP request rate: no more than one request per `rate_limit_seconds` (default 2), with random jitter. User agent must not impersonate a browser.
- TDD: every task writes the failing test first. Commit after every task.
- Package source lives under `src/daftwatch/`; tests under `tests/`.

---

### Task 1: Project scaffold and configuration loader

**Files:**
- Create: `src/daftwatch/__init__.py` (empty)
- Create: `src/daftwatch/config.py`
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_config.py`
- Create: `requirements.txt`
- Create: `pyproject.toml`
- Create: `config.example.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Search` dataclass: `name: str`, `category: str`, `params: dict`.
  - `NotifyConfig` dataclass: `min_event_types: list[str]`.
  - `Config` dataclass: `interval_minutes: int`, `gone_after_cycles: int`, `rate_limit_seconds: float`, `searches: list[Search]`, `filters: dict`, `notify: NotifyConfig`.
  - `SmtpConfig` dataclass: `host: str`, `port: int`, `user: str`, `password: str`, `sender: str`, `recipient: str`; classmethod `from_env(env: Mapping[str, str]) -> SmtpConfig` (raises `KeyError` if any var missing).
  - `load_config(path: str | os.PathLike) -> Config`.

- [ ] **Step 1: Write `pyproject.toml` and `requirements.txt`**

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "daftwatch"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["daftlistings==0.16", "PyYAML==6.0.2"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
markers = ["live: hits the real daft.ie site; excluded from CI"]
```

`requirements.txt`:

```
daftlistings==0.16
PyYAML==6.0.2
pytest==8.3.3
```

Note: if `daftlistings==0.16` is not installable, pick the newest version that is and update both files identically.

- [ ] **Step 2: Write the failing test**

`tests/test_config.py`:

```python
import textwrap
import pytest
from daftwatch.config import load_config, SmtpConfig


def test_load_config_parses_searches_and_notify(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        interval_minutes: 15
        gone_after_cycles: 3
        rate_limit_seconds: 2
        searches:
          - name: "Dublin rent"
            category: rent
            params: { location: [dublin-city], max_price: 2200 }
          - name: "Sharing"
            category: sharing
            params: { location: [dublin-8-dublin] }
        filters:
          keywords_exclude: [student]
        notify:
          min_event_types: [NEW, PRICE_DROP]
    """))
    cfg = load_config(p)
    assert cfg.interval_minutes == 15
    assert cfg.gone_after_cycles == 3
    assert [s.name for s in cfg.searches] == ["Dublin rent", "Sharing"]
    assert cfg.searches[0].category == "rent"
    assert cfg.searches[0].params["max_price"] == 2200
    assert cfg.filters["keywords_exclude"] == ["student"]
    assert cfg.notify.min_event_types == ["NEW", "PRICE_DROP"]


def test_load_config_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("searches: []\n")
    cfg = load_config(p)
    assert cfg.interval_minutes == 30
    assert cfg.gone_after_cycles == 2
    assert cfg.rate_limit_seconds == 2
    assert cfg.filters == {}
    assert cfg.notify.min_event_types == ["NEW", "PRICE_DROP", "GONE"]


def test_load_config_rejects_unknown_category(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("searches:\n  - name: x\n    category: sale\n    params: {}\n")
    with pytest.raises(ValueError, match="category"):
        load_config(p)


def test_smtp_config_from_env_missing_var():
    with pytest.raises(KeyError):
        SmtpConfig.from_env({"SMTP_HOST": "h"})


def test_smtp_config_from_env_ok():
    env = {
        "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
        "SMTP_USER": "u", "SMTP_PASS": "p",
        "ALERT_FROM": "from@example.com", "ALERT_TO": "to@example.com",
    }
    s = SmtpConfig.from_env(env)
    assert s.host == "smtp.example.com"
    assert s.port == 587
    assert s.recipient == "to@example.com"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'daftwatch.config'`

- [ ] **Step 4: Write `src/daftwatch/config.py`**

```python
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

import yaml

VALID_CATEGORIES = {"rent", "sharing"}
DEFAULT_MIN_EVENT_TYPES = ["NEW", "PRICE_DROP", "GONE"]


@dataclass
class Search:
    name: str
    category: str
    params: dict


@dataclass
class NotifyConfig:
    min_event_types: list[str] = field(
        default_factory=lambda: list(DEFAULT_MIN_EVENT_TYPES)
    )


@dataclass
class Config:
    interval_minutes: int = 30
    gone_after_cycles: int = 2
    rate_limit_seconds: float = 2.0
    searches: list[Search] = field(default_factory=list)
    filters: dict = field(default_factory=dict)
    notify: NotifyConfig = field(default_factory=NotifyConfig)


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipient: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SmtpConfig":
        return cls(
            host=env["SMTP_HOST"],
            port=int(env["SMTP_PORT"]),
            user=env["SMTP_USER"],
            password=env["SMTP_PASS"],
            sender=env["ALERT_FROM"],
            recipient=env["ALERT_TO"],
        )


def load_config(path: str | os.PathLike) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    searches: list[Search] = []
    for raw in data.get("searches", []):
        category = raw["category"] if "category" in raw else raw.get("category", "")
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"search {raw.get('name')!r} has category {category!r}; "
                f"must be one of {sorted(VALID_CATEGORIES)}"
            )
        searches.append(
            Search(name=raw["name"], category=category, params=raw.get("params", {}))
        )

    notify_raw = data.get("notify", {}) or {}
    notify = NotifyConfig(
        min_event_types=notify_raw.get(
            "min_event_types", list(DEFAULT_MIN_EVENT_TYPES)
        )
    )

    return Config(
        interval_minutes=data.get("interval_minutes", 30),
        gone_after_cycles=data.get("gone_after_cycles", 2),
        rate_limit_seconds=data.get("rate_limit_seconds", 2.0),
        searches=searches,
        filters=data.get("filters", {}) or {},
        notify=notify,
    )
```

- [ ] **Step 5: Write `config.example.yaml`**

```yaml
interval_minutes: 30
gone_after_cycles: 2
rate_limit_seconds: 2

searches:
  - name: "Dublin city rent, 1+ bed, <= 2200"
    category: rent
    params:
      location: [dublin-city]
      min_beds: 1
      max_price: 2200
  - name: "House share D6 / D8, <= 900"
    category: sharing
    params:
      location: [dublin-6-dublin, dublin-8-dublin]
      max_price: 900

# Deferred: matching rules. Empty = every listing passes.
filters:
  keywords_exclude: []

notify:
  min_event_types: [NEW, PRICE_DROP, GONE]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml requirements.txt config.example.yaml src/daftwatch tests
git commit -m "feat: project scaffold and config loader"
```

---

### Task 2: Listing model and price parsing

**Files:**
- Create: `src/daftwatch/models.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Listing` frozen dataclass with fields: `id: str`, `category: str`, `title: str`, `url: str`, `price_eur: int` (monthly; `0` means "price on application / unknown"), `beds: int | None`, `baths: int | None`, `property_type: str | None`, `area: str | None`, `county: str | None`, `lat: float | None`, `lng: float | None`, `raw: dict`.
  - `parse_price(text: str) -> int` — returns monthly euros, `0` when not a number.
  - `parse_beds(text: str | int | None) -> int | None`.

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:

```python
from daftwatch.models import Listing, parse_price, parse_beds


def test_parse_price_monthly():
    assert parse_price("€2,200 per month") == 2200


def test_parse_price_weekly_converts_to_monthly():
    assert parse_price("€500 per week") == round(500 * 52 / 12)


def test_parse_price_plain_number():
    assert parse_price("€1750") == 1750


def test_parse_price_on_application():
    assert parse_price("Price on application") == 0


def test_parse_price_empty():
    assert parse_price("") == 0


def test_parse_beds_from_string():
    assert parse_beds("2 Bed") == 2


def test_parse_beds_from_int():
    assert parse_beds(3) == 3


def test_parse_beds_none():
    assert parse_beds(None) is None
    assert parse_beds("Studio") is None


def test_listing_is_frozen():
    l = Listing(
        id="1", category="rent", title="t", url="u", price_eur=1000,
        beds=1, baths=1, property_type="Apartment", area="D8",
        county="Dublin", lat=None, lng=None, raw={},
    )
    try:
        l.id = "2"
    except Exception as e:
        assert "frozen" in str(type(e)).lower() or "cannot assign" in str(e).lower()
    else:
        raise AssertionError("Listing should be frozen")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'daftwatch.models'`

- [ ] **Step 3: Write `src/daftwatch/models.py`**

```python
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
```

Note: `parse_price` strips commas before matching, so the regex sees `2200` not `2,200`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_models.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/daftwatch/models.py tests/test_models.py
git commit -m "feat: Listing model and price/beds parsing"
```

---

### Task 3: SQLite store — schema, upsert, NEW / PRICE / BACK events

**Files:**
- Create: `src/daftwatch/store.py`
- Create: `tests/test_store.py`

**Interfaces:**
- Consumes: `Listing` from `daftwatch.models`.
- Produces:
  - `Event` frozen dataclass: `id: int | None`, `listing_id: str`, `type: str` (`NEW` / `PRICE_DROP` / `PRICE_UP` / `GONE` / `BACK`), `old_price: int | None`, `new_price: int | None`.
  - `Store` class:
    - `Store(path: str)` — opens/creates the DB, creates tables if absent.
    - `begin_cycle() -> None` — clears the in-memory "seen this cycle" set.
    - `sync(search_name: str, listings: list[Listing]) -> list[Event]` — upserts each listing, returns NEW / PRICE_DROP / PRICE_UP / BACK events created; marks each listing id seen this cycle.
    - `get_listing(listing_id: str) -> Listing | None` — reads the stored row back as a `Listing` (`raw` will be `{}`).
    - `close() -> None`.
  - Table definitions exactly as in the spec's Data model section, plus `listings.missing_cycles INTEGER NOT NULL DEFAULT 0`.

- [ ] **Step 1: Write the failing test**

`tests/test_store.py`:

```python
import pytest
from daftwatch.models import Listing
from daftwatch.store import Store


def mk(id="1", price=1000, category="rent"):
    return Listing(
        id=id, category=category, title=f"Flat {id}", url=f"https://daft.ie/{id}",
        price_eur=price, beds=2, baths=1, property_type="Apartment",
        area="D8", county="Dublin", lat=53.3, lng=-6.2, raw={"x": 1},
    )


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def test_first_sight_emits_new(store):
    store.begin_cycle()
    events = store.sync("s1", [mk("1")])
    assert len(events) == 1
    assert events[0].type == "NEW"
    assert events[0].listing_id == "1"
    assert events[0].id is not None


def test_unchanged_listing_emits_nothing(store):
    store.begin_cycle()
    store.sync("s1", [mk("1", price=1000)])
    store.begin_cycle()
    events = store.sync("s1", [mk("1", price=1000)])
    assert events == []


def test_price_drop_and_rise(store):
    store.begin_cycle()
    store.sync("s1", [mk("1", price=1000)])
    store.begin_cycle()
    drop = store.sync("s1", [mk("1", price=900)])
    assert drop[0].type == "PRICE_DROP"
    assert (drop[0].old_price, drop[0].new_price) == (1000, 900)
    store.begin_cycle()
    rise = store.sync("s1", [mk("1", price=950)])
    assert rise[0].type == "PRICE_UP"


def test_get_listing_roundtrip(store):
    store.begin_cycle()
    store.sync("s1", [mk("1", price=1234)])
    got = store.get_listing("1")
    assert got.price_eur == 1234
    assert got.url == "https://daft.ie/1"
    assert store.get_listing("nope") is None


def test_seen_set_resets_each_cycle(store):
    store.begin_cycle()
    store.sync("s1", [mk("1")])
    store.begin_cycle()
    # not passing "1" this cycle; sync of a different search
    store.sync("s2", [mk("2")])
    # "1" was not seen this cycle -> exposed via internal API for Task 4
    assert store.seen_this_cycle() == {"2"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'daftwatch.store'`

- [ ] **Step 3: Write `src/daftwatch/store.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_store.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/daftwatch/store.py tests/test_store.py
git commit -m "feat: SQLite store with NEW/PRICE/BACK diff events"
```

---

### Task 4: Store — GONE sweep and notification bookkeeping

**Files:**
- Modify: `src/daftwatch/store.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- Consumes: `Event`, `Store` from Task 3.
- Produces (new `Store` methods):
  - `finish_cycle(gone_after_cycles: int) -> list[Event]` — for every `active=1` listing whose id is NOT in `seen_this_cycle()`, increment `missing_cycles`; when it reaches `gone_after_cycles`, set `active=0` and emit a `GONE` event (`old_price` = stored price, `new_price` = None). Returns the GONE events. Must be called once per cycle after all `sync` calls.
  - `pending_events() -> list[Event]` — every event with no row in `notified`, oldest first, `id` populated.
  - `mark_notified(event_ids: list[int]) -> None` — insert a `notified` row (`sent_at` = now) for each id; ignore ids already present.

- [ ] **Step 1: Write the failing tests (append to `tests/test_store.py`)**

```python
def test_gone_after_threshold(store):
    store.begin_cycle()
    store.sync("s1", [mk("1")])
    # cycle 2: absent -> missing_cycles = 1, no event yet
    store.begin_cycle()
    store.sync("s1", [])
    assert store.finish_cycle(2) == []
    # cycle 3: absent -> missing_cycles = 2 -> GONE
    store.begin_cycle()
    store.sync("s1", [])
    events = store.finish_cycle(2)
    assert len(events) == 1
    assert events[0].type == "GONE"
    assert events[0].listing_id == "1"


def test_reappear_after_gone_emits_back(store):
    store.begin_cycle(); store.sync("s1", [mk("1", price=1000)])
    store.begin_cycle(); store.sync("s1", []); store.finish_cycle(1)  # GONE now
    store.begin_cycle()
    back = store.sync("s1", [mk("1", price=1000)])
    assert back[0].type == "BACK"


def test_pending_events_and_mark_notified(store):
    store.begin_cycle()
    evts = store.sync("s1", [mk("1"), mk("2")])
    pending = store.pending_events()
    assert {e.listing_id for e in pending} == {"1", "2"}
    store.mark_notified([pending[0].id])
    remaining = store.pending_events()
    assert [e.listing_id for e in remaining] == [pending[1].listing_id]
    # idempotent
    store.mark_notified([pending[0].id])
    assert len(store.pending_events()) == 1


def test_seen_this_cycle_across_multiple_searches(store):
    store.begin_cycle()
    store.sync("s1", [mk("1")])
    store.sync("s2", [mk("1"), mk("2")])
    store.begin_cycle()
    store.sync("s1", [mk("1")])       # "1" seen via s1
    store.sync("s2", [])              # s2 empty this cycle
    gone = store.finish_cycle(1)
    assert {e.listing_id for e in gone} == {"2"}   # only "2" fully absent
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_store.py -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'finish_cycle'`

- [ ] **Step 3: Add methods to `src/daftwatch/store.py`**

Add inside the `Store` class:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_store.py -v`
Expected: PASS (9 tests total in the file)

- [ ] **Step 5: Commit**

```bash
git add src/daftwatch/store.py tests/test_store.py
git commit -m "feat: GONE sweep and notified bookkeeping"
```

---

### Task 5: Filters

**Files:**
- Create: `src/daftwatch/filters.py`
- Create: `tests/test_filters.py`

**Interfaces:**
- Consumes: `Listing` from `daftwatch.models`.
- Produces: `apply(listings: list[Listing], filter_config: dict) -> list[Listing]`. Supported keys (all optional): `keywords_exclude: list[str]` (drop a listing whose title contains any, case-insensitive). Unknown keys are ignored. Empty/`{}` config returns the input unchanged (same order).

- [ ] **Step 1: Write the failing test**

`tests/test_filters.py`:

```python
from daftwatch.models import Listing
from daftwatch.filters import apply


def mk(id, title):
    return Listing(id=id, category="rent", title=title, url="u", price_eur=1000,
                   beds=1, baths=1, property_type=None, area=None, county=None,
                   lat=None, lng=None, raw={})


def test_empty_config_passes_everything():
    ls = [mk("1", "Nice flat"), mk("2", "Student digs")]
    assert apply(ls, {}) == ls


def test_keywords_exclude_case_insensitive():
    ls = [mk("1", "Nice flat"), mk("2", "STUDENT accommodation"),
          mk("3", "short term let")]
    out = apply(ls, {"keywords_exclude": ["student", "short term"]})
    assert [l.id for l in out] == ["1"]


def test_unknown_keys_ignored():
    ls = [mk("1", "flat")]
    assert apply(ls, {"nonsense": True}) == ls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_filters.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'daftwatch.filters'`

- [ ] **Step 3: Write `src/daftwatch/filters.py`**

```python
from __future__ import annotations

from daftwatch.models import Listing


def apply(listings: list[Listing], filter_config: dict) -> list[Listing]:
    excludes = [w.lower() for w in (filter_config or {}).get("keywords_exclude", [])]
    if not excludes:
        return list(listings)
    out = []
    for l in listings:
        title = (l.title or "").lower()
        if any(word in title for word in excludes):
            continue
        out.append(l)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_filters.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/daftwatch/filters.py tests/test_filters.py
git commit -m "feat: keyword-exclude filter"
```

---

### Task 6: Search adapter interface and daftlistings mapping

**Files:**
- Create: `src/daftwatch/adapter.py`
- Create: `tests/test_adapter.py`
- Create: `tests/fixtures/__init__.py` (empty)
- Create: `tests/fixtures/daft_rent_page1.json`

**Interfaces:**
- Consumes: `Listing`, `parse_price`, `parse_beds` from `daftwatch.models`; `Search` from `daftwatch.config`.
- Produces:
  - `AdapterError(Exception)` — raised when the underlying library fails in an unexpected way, or after backoff is exhausted.
  - `SearchAdapter` ABC with `fetch(self, search: Search) -> list[Listing]`.
  - `to_listing(d: dict, category: str) -> Listing` — maps one `daftlistings` `.as_dict()` result to a `Listing`. Expected input keys (missing keys tolerated, default `None`): `id`, `title`, `price` (string), `bedrooms`, `bathrooms`, `category` (property type string), `daft_link` / `url`, `latitude`, `longitude`, and `location`/`area` for area text. `price_eur` via `parse_price`; `beds` via `parse_beds`.

- [ ] **Step 1: Create the fixture**

`tests/fixtures/daft_rent_page1.json` — a JSON array of two objects shaped like `daftlistings` `.as_dict()` output:

```json
[
  {
    "id": "5001",
    "title": "2 Bed Apartment, Rialto, Dublin 8",
    "price": "€2,100 per month",
    "bedrooms": "2 Bed",
    "bathrooms": "1 Bath",
    "category": "Apartment",
    "daft_link": "https://www.daft.ie/for-rent/apartment-rialto-dublin-8/5001",
    "latitude": 53.3331,
    "longitude": -6.2925,
    "location": "Rialto, Dublin 8"
  },
  {
    "id": "5002",
    "title": "Studio, Portobello",
    "price": "€400 per week",
    "bedrooms": "Studio",
    "bathrooms": "1 Bath",
    "category": "Studio",
    "daft_link": "https://www.daft.ie/for-rent/studio-portobello/5002",
    "latitude": 53.33,
    "longitude": -6.26,
    "location": "Portobello, Dublin 8"
  }
]
```

Note: after the first real run, replace this with genuine `.as_dict()` output captured from `daftlistings` and adjust `to_listing` key names to match. Keep the two-item shape (one monthly, one weekly).

- [ ] **Step 2: Write the failing test**

`tests/test_adapter.py`:

```python
import json
from pathlib import Path

import pytest
from daftwatch.adapter import SearchAdapter, AdapterError, to_listing

FIX = Path(__file__).parent / "fixtures"


def test_to_listing_monthly():
    d = json.loads((FIX / "daft_rent_page1.json").read_text())[0]
    l = to_listing(d, "rent")
    assert l.id == "5001"
    assert l.category == "rent"
    assert l.price_eur == 2100
    assert l.beds == 2
    assert l.property_type == "Apartment"
    assert l.url.endswith("/5001")
    assert l.lat == 53.3331


def test_to_listing_weekly_converts():
    d = json.loads((FIX / "daft_rent_page1.json").read_text())[1]
    l = to_listing(d, "rent")
    assert l.price_eur == round(400 * 52 / 12)
    assert l.beds is None


def test_to_listing_tolerates_missing_keys():
    l = to_listing({"id": "9"}, "sharing")
    assert l.id == "9"
    assert l.price_eur == 0
    assert l.beds is None
    assert l.url == ""


def test_searchadapter_is_abstract():
    with pytest.raises(TypeError):
        SearchAdapter()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'daftwatch.adapter'`

- [ ] **Step 4: Write `src/daftwatch/adapter.py` (interface + mapping only)**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_adapter.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add src/daftwatch/adapter.py tests/test_adapter.py tests/fixtures
git commit -m "feat: SearchAdapter interface and daftlistings mapping"
```

---

### Task 7: DaftListingsAdapter — fetch, pagination, rate limit, backoff

**Files:**
- Modify: `src/daftwatch/adapter.py`
- Modify: `tests/test_adapter.py`

**Interfaces:**
- Consumes: `SearchAdapter`, `to_listing`, `AdapterError` from Task 6.
- Produces:
  - `DaftListingsAdapter(rate_limit_seconds: float = 2.0, max_pages: int = 20, sleeper: Callable[[float], None] = time.sleep, client_factory: Callable[[], Any] = _default_client)` implementing `fetch`.
  - `_default_client() -> daftlistings.Daft` — constructs a `Daft` search object. Isolated so tests inject a fake.
  - `fetch` behavior: translate `search.category` (`rent` → residential rent, `sharing` → sharing) and `search.params` keys (`location: list[str]`, `min_beds: int`, `max_beds: int`, `min_price: int`, `max_price: int`) onto the client; page until a page returns fewer than the previous page's count or `max_pages` is hit or a page is empty; sleep `rate_limit_seconds` (± up to 0.5 jitter) between requests via `sleeper`; map results with `to_listing`.
  - On a client raising an exception whose text contains `429` or `403`: retry with exponential backoff `30, 60, 120, 240, 300` seconds (cap 300) via `sleeper`, max 5 retries, then raise `AdapterError`.
  - On any other client exception: wrap in `AdapterError`.

The fake client contract the tests rely on (your `_default_client` must expose the same methods, delegating to `daftlistings`):

```
client.set_category(category: str)          # "rent" | "sharing"
client.set_params(params: dict)             # location/min_beds/.../max_price
client.page(n: int) -> list[dict]           # 1-indexed; [] past the end
```

Wrap `daftlistings` inside `_default_client`'s returned object so this contract holds; the real translation of these calls into `daftlistings` API lives there and nowhere else.

- [ ] **Step 1: Write the failing tests (append to `tests/test_adapter.py`)**

```python
from daftwatch.adapter import DaftListingsAdapter
from daftwatch.config import Search


class FakeClient:
    def __init__(self, pages):
        self._pages = pages
        self.category = None
        self.params = None
        self.calls = []

    def set_category(self, c): self.category = c
    def set_params(self, p): self.params = p

    def page(self, n):
        self.calls.append(n)
        return self._pages[n - 1] if n - 1 < len(self._pages) else []


class BoomClient:
    def __init__(self, msg): self.msg = msg
    def set_category(self, c): pass
    def set_params(self, p): pass
    def page(self, n): raise RuntimeError(self.msg)


def _rec_sleeper():
    slept = []
    return slept, (lambda s: slept.append(s))


def test_fetch_paginates_and_maps():
    p1 = [{"id": "1", "price": "€1000 per month"},
          {"id": "2", "price": "€1100 per month"}]
    p2 = [{"id": "3", "price": "€1200 per month"}]   # shorter -> last page
    fake = FakeClient([p1, p2])
    slept, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(rate_limit_seconds=2, sleeper=sleeper,
                            client_factory=lambda: fake)
    out = a.fetch(Search(name="s", category="rent", params={"max_price": 1500}))
    assert [l.id for l in out] == ["1", "2", "3"]
    assert fake.category == "rent"
    assert fake.params == {"max_price": 1500}
    assert fake.calls == [1, 2]
    assert len(slept) >= 1 and all(1.5 <= s <= 2.5 for s in slept)


def test_fetch_stops_at_max_pages():
    full = [{"id": str(i), "price": "€1 per month"} for i in range(3)]
    fake = FakeClient([full] * 50)
    _, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(sleeper=sleeper, max_pages=4, client_factory=lambda: fake)
    a.fetch(Search(name="s", category="rent", params={}))
    assert fake.calls == [1, 2, 3, 4]


def test_fetch_backoff_then_error_on_429():
    slept, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(sleeper=sleeper,
                            client_factory=lambda: BoomClient("HTTP 429 Too Many"))
    with pytest.raises(AdapterError):
        a.fetch(Search(name="s", category="rent", params={}))
    assert slept[:5] == [30, 60, 120, 240, 300]


def test_fetch_wraps_other_errors():
    a = DaftListingsAdapter(sleeper=lambda s: None,
                            client_factory=lambda: BoomClient("boom"))
    with pytest.raises(AdapterError):
        a.fetch(Search(name="s", category="rent", params={}))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_adapter.py -v`
Expected: FAIL — `ImportError: cannot import name 'DaftListingsAdapter'`

- [ ] **Step 3: Add to `src/daftwatch/adapter.py`**

```python
import random
import time
from collections.abc import Callable
from typing import Any

_BACKOFF = [30, 60, 120, 240, 300]


def _default_client() -> Any:
    """Adapter-shaped wrapper around daftlistings.

    This is the ONLY place daftlistings API details live. If daftlistings
    changes, rewrite this function and nothing else.
    """
    from daftlistings import Daft, SearchType

    class _Client:
        def __init__(self):
            self._daft = Daft()
            self._type = SearchType.RESIDENTIAL_RENT

        def set_category(self, c: str) -> None:
            self._type = (
                SearchType.SHARING if c == "sharing"
                else SearchType.RESIDENTIAL_RENT
            )
            self._daft.set_search_type(self._type)

        def set_params(self, p: dict) -> None:
            for loc in p.get("location", []):
                self._daft.set_location(loc)
            if "min_price" in p:
                self._daft.set_min_price(p["min_price"])
            if "max_price" in p:
                self._daft.set_max_price(p["max_price"])
            if "min_beds" in p:
                self._daft.set_min_beds(p["min_beds"])
            if "max_beds" in p:
                self._daft.set_max_beds(p["max_beds"])

        def page(self, n: int) -> list[dict]:
            results = self._daft.search(max_pages=1, page=n)
            return [r.as_dict() for r in results]

    return _Client()


class DaftListingsAdapter(SearchAdapter):
    def __init__(
        self,
        rate_limit_seconds: float = 2.0,
        max_pages: int = 20,
        sleeper: Callable[[float], None] = time.sleep,
        client_factory: Callable[[], Any] = _default_client,
    ):
        self._rate = rate_limit_seconds
        self._max_pages = max_pages
        self._sleep = sleeper
        self._make_client = client_factory

    def _page_with_backoff(self, client: Any, n: int) -> list[dict]:
        for delay in _BACKOFF:
            try:
                return client.page(n)
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                if "429" in text or "403" in text:
                    self._sleep(delay)
                    continue
                raise AdapterError(f"daftlistings failed: {exc!r}") from exc
        raise AdapterError("rate-limited by daft.ie; backoff exhausted")

    def fetch(self, search: Search) -> list[Listing]:
        client = self._make_client()
        client.set_category(search.category)
        client.set_params(dict(search.params))

        listings: list[Listing] = []
        prev_count: int | None = None
        for n in range(1, self._max_pages + 1):
            if n > 1:
                self._sleep(self._rate + random.uniform(-0.5, 0.5))
            page = self._page_with_backoff(client, n)
            if not page:
                break
            listings.extend(to_listing(d, search.category) for d in page)
            if prev_count is not None and len(page) < prev_count:
                break
            prev_count = len(page)
        return listings
```

Note on the first-request sleep: the test expects at least one sleep for a two-page fetch, and sleeps only happen before pages 2..N. `test_fetch_paginates_and_maps` has two pages so one sleep — matches. If you add a pre-first-request sleep, update that test's `>= 1` reasoning accordingly; simpler to keep sleeps between pages only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_adapter.py -v`
Expected: PASS (8 tests total in the file)

- [ ] **Step 5: Commit**

```bash
git add src/daftwatch/adapter.py tests/test_adapter.py
git commit -m "feat: DaftListingsAdapter with pagination, rate limit, backoff"
```

---

### Task 8: Email notifier

**Files:**
- Create: `src/daftwatch/notify.py`
- Create: `tests/test_notify.py`

**Interfaces:**
- Consumes: `SmtpConfig` from `daftwatch.config`; `Event` from `daftwatch.store`; `Listing` from `daftwatch.models`.
- Produces:
  - `EmailNotifier(smtp: SmtpConfig, smtplib_module=smtplib)` — `smtplib_module` injectable for tests.
  - `send_digest(items: list[tuple[Event, Listing]]) -> None` — builds one plain-text email summarising all items and sends it. No-op if `items` is empty. Subject: `"[daft-watch] N update(s): X new, Y price drop(s), Z gone"`.
  - `send_alert(subject: str, body: str) -> None` — sends a one-off plain-text email with subject prefixed `"[daft-watch] "`.
  - Both use STARTTLS: `SMTP(host, port)` → `starttls()` → `login(user, password)` → `send_message(msg)` → `quit()`.
  - Raises whatever `smtplib` raises (caller handles retry).

- [ ] **Step 1: Write the failing test**

`tests/test_notify.py`:

```python
from daftwatch.config import SmtpConfig
from daftwatch.models import Listing
from daftwatch.store import Event
from daftwatch.notify import EmailNotifier


class FakeSMTP:
    instances = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.tls = False
        self.logged_in = None
        self.sent = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self): self.tls = True
    def login(self, u, p): self.logged_in = (u, p)
    def send_message(self, msg): self.sent.append(msg)
    def quit(self): self.quit_called = True


class FakeSmtplib:
    SMTP = FakeSMTP


def smtp_cfg():
    return SmtpConfig(host="h", port=587, user="u", password="pw",
                      sender="from@x.com", recipient="to@x.com")


def mk_listing(id, price):
    return Listing(id=id, category="rent", title=f"Flat {id}",
                   url=f"https://daft.ie/{id}", price_eur=price, beds=2, baths=1,
                   property_type="Apartment", area="D8", county="Dublin",
                   lat=None, lng=None, raw={})


def setup_function():
    FakeSMTP.instances.clear()


def test_send_digest_builds_one_email():
    items = [
        (Event(1, "1", "NEW", None, 2000), mk_listing("1", 2000)),
        (Event(2, "2", "PRICE_DROP", 2200, 2000), mk_listing("2", 2000)),
    ]
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_digest(items)
    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert smtp.tls and smtp.logged_in == ("u", "pw") and smtp.quit_called
    msg = smtp.sent[0]
    assert msg["To"] == "to@x.com"
    assert msg["From"] == "from@x.com"
    assert "2 update(s)" in msg["Subject"]
    assert "1 new" in msg["Subject"]
    assert "1 price drop(s)" in msg["Subject"]
    body = msg.get_content()
    assert "https://daft.ie/1" in body
    assert "2200" in body and "2000" in body


def test_send_digest_empty_is_noop():
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_digest([])
    assert FakeSMTP.instances == []


def test_send_alert_prefixes_subject():
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_alert("scraper broken", "traceback here")
    msg = FakeSMTP.instances[0].sent[0]
    assert msg["Subject"] == "[daft-watch] scraper broken"
    assert "traceback here" in msg.get_content()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_notify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'daftwatch.notify'`

- [ ] **Step 3: Write `src/daftwatch/notify.py`**

```python
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from daftwatch.config import SmtpConfig
from daftwatch.models import Listing
from daftwatch.store import Event

_LABEL = {
    "NEW": "new", "PRICE_DROP": "price drop(s)", "PRICE_UP": "price rise(s)",
    "GONE": "gone", "BACK": "relisted",
}


class EmailNotifier:
    def __init__(self, smtp: SmtpConfig, smtplib_module=smtplib):
        self._smtp = smtp
        self._smtplib = smtplib_module

    def _send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._smtp.sender
        msg["To"] = self._smtp.recipient
        msg["Subject"] = subject
        msg.set_content(body)
        server = self._smtplib.SMTP(self._smtp.host, self._smtp.port)
        try:
            server.starttls()
            server.login(self._smtp.user, self._smtp.password)
            server.send_message(msg)
        finally:
            server.quit()

    def send_alert(self, subject: str, body: str) -> None:
        self._send(f"[daft-watch] {subject}", body)

    def send_digest(self, items: list[tuple[Event, Listing]]) -> None:
        if not items:
            return
        counts: dict[str, int] = {}
        for event, _ in items:
            counts[event.type] = counts.get(event.type, 0) + 1
        parts = [
            f"{counts[t]} {_LABEL.get(t, t.lower())}"
            for t in ("NEW", "PRICE_DROP", "PRICE_UP", "GONE", "BACK")
            if t in counts
        ]
        subject = (
            f"[daft-watch] {len(items)} update(s): " + ", ".join(parts)
        )

        lines: list[str] = []
        for event, listing in items:
            lines.append(f"[{event.type}] {listing.title}")
            if event.type in ("PRICE_DROP", "PRICE_UP"):
                lines.append(
                    f"  price: {event.old_price} -> {event.new_price} EUR/month"
                )
            else:
                lines.append(f"  price: {listing.price_eur} EUR/month")
            beds = "?" if listing.beds is None else listing.beds
            lines.append(f"  {beds} bed | {listing.area or '-'}")
            lines.append(f"  {listing.url}")
            lines.append("")
        self._send(subject, "\n".join(lines))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_notify.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/daftwatch/notify.py tests/test_notify.py
git commit -m "feat: email notifier with digest and alert"
```

---

### Task 9: Runner — one cycle

**Files:**
- Create: `src/daftwatch/runner.py`
- Create: `tests/test_runner.py`

**Interfaces:**
- Consumes: `Config` from `daftwatch.config`; `Store` from `daftwatch.store`; `SearchAdapter`, `AdapterError` from `daftwatch.adapter`; `EmailNotifier` from `daftwatch.notify`; `filters.apply`.
- Produces:
  - `run_cycle(config: Config, store: Store, adapter: SearchAdapter, notifier: EmailNotifier, logger: logging.Logger) -> CycleResult`.
  - `CycleResult` dataclass: `events_sent: int`, `searches_failed: list[str]`, `adapter_broken: bool` (True if any search raised `AdapterError`).
  - Behavior: `store.begin_cycle()`; for each search, `adapter.fetch` — on `AdapterError` log it, append name to `searches_failed`, set `adapter_broken`, continue; collect all fetched listings; `store.sync` per search; then `store.finish_cycle`; gather `store.pending_events()`; keep events whose `type` is in `config.notify.min_event_types` AND (for non-GONE) whose `listing_id` survived `filters.apply` on the fetched listings (GONE events always pass the filter stage — the listing is gone, nothing to match); pair each kept event with its listing via `store.get_listing`; `notifier.send_digest`; on success `store.mark_notified` with those event ids. A `send_digest` exception propagates out of `run_cycle` after logging (caller decides).

- [ ] **Step 1: Write the failing test**

`tests/test_runner.py`:

```python
import logging

from daftwatch.adapter import AdapterError, SearchAdapter
from daftwatch.config import Config, NotifyConfig, Search
from daftwatch.models import Listing
from daftwatch.store import Store
from daftwatch.runner import run_cycle


def mk(id, price, title="Flat"):
    return Listing(id=id, category="rent", title=title, url=f"https://d/{id}",
                   price_eur=price, beds=2, baths=1, property_type="Apartment",
                   area="D8", county="Dublin", lat=None, lng=None, raw={})


class FakeAdapter(SearchAdapter):
    def __init__(self, by_search): self.by_search = by_search
    def fetch(self, search):
        v = self.by_search[search.name]
        if isinstance(v, Exception):
            raise v
        return v


class RecordingNotifier:
    def __init__(self): self.digests = []
    def send_digest(self, items): self.digests.append(list(items))
    def send_alert(self, s, b): pass


def cfg(searches, min_types=("NEW", "PRICE_DROP", "GONE"), filters=None):
    return Config(searches=list(searches), gone_after_cycles=1,
                  filters=filters or {},
                  notify=NotifyConfig(min_event_types=list(min_types)))


def test_new_listings_are_notified_once(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="s1", category="rent", params={})
    adapter = FakeAdapter({"s1": [mk("1", 2000), mk("2", 2100)]})
    notifier = RecordingNotifier()
    log = logging.getLogger("test")

    r1 = run_cycle(cfg([s]), store, adapter, notifier, log)
    assert r1.events_sent == 2
    assert len(notifier.digests[0]) == 2

    # second cycle, same listings -> nothing new
    r2 = run_cycle(cfg([s]), store, adapter, notifier, log)
    assert r2.events_sent == 0
    assert len(notifier.digests) == 1  # no second email
    store.close()


def test_adapter_error_is_isolated(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s1 = Search(name="s1", category="rent", params={})
    s2 = Search(name="s2", category="sharing", params={})
    adapter = FakeAdapter({"s1": AdapterError("boom"), "s2": [mk("9", 800)]})
    notifier = RecordingNotifier()
    r = run_cycle(cfg([s1, s2]), store, adapter, notifier, logging.getLogger("t"))
    assert r.searches_failed == ["s1"]
    assert r.adapter_broken is True
    assert r.events_sent == 1  # s2 still processed
    store.close()


def test_filter_excludes_from_notification(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="s1", category="rent", params={})
    adapter = FakeAdapter({"s1": [mk("1", 2000, "Nice flat"),
                                  mk("2", 2000, "Student room")]})
    notifier = RecordingNotifier()
    r = run_cycle(cfg([s], filters={"keywords_exclude": ["student"]}),
                  store, adapter, notifier, logging.getLogger("t"))
    assert r.events_sent == 1
    assert notifier.digests[0][0][1].id == "1"
    store.close()


def test_min_event_types_gate(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="s1", category="rent", params={})
    notifier = RecordingNotifier()
    # cycle 1: NEW at 2000
    run_cycle(cfg([s], min_types=("PRICE_DROP",)), store,
              FakeAdapter({"s1": [mk("1", 2000)]}), notifier, logging.getLogger("t"))
    assert notifier.digests == []  # NEW not in min_types
    # cycle 2: price drop -> notified
    r = run_cycle(cfg([s], min_types=("PRICE_DROP",)), store,
                  FakeAdapter({"s1": [mk("1", 1800)]}), notifier,
                  logging.getLogger("t"))
    assert r.events_sent == 1
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'daftwatch.runner'`

- [ ] **Step 3: Write `src/daftwatch/runner.py`**

```python
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from daftwatch import filters
from daftwatch.adapter import AdapterError, SearchAdapter
from daftwatch.config import Config
from daftwatch.notify import EmailNotifier
from daftwatch.store import Store


@dataclass
class CycleResult:
    events_sent: int = 0
    searches_failed: list[str] = field(default_factory=list)
    adapter_broken: bool = False


def run_cycle(
    config: Config,
    store: Store,
    adapter: SearchAdapter,
    notifier: EmailNotifier,
    logger: logging.Logger,
) -> CycleResult:
    result = CycleResult()
    store.begin_cycle()

    fetched: list = []
    for search in config.searches:
        try:
            listings = adapter.fetch(search)
        except AdapterError as exc:
            logger.error("search %r failed: %r", search.name, exc)
            result.searches_failed.append(search.name)
            result.adapter_broken = True
            continue
        fetched.extend(listings)
        store.sync(search.name, listings)

    store.finish_cycle(config.gone_after_cycles)

    allowed_ids = {l.id for l in filters.apply(fetched, config.filters)}
    min_types = set(config.notify.min_event_types)

    to_send: list[tuple] = []
    for event in store.pending_events():
        if event.type not in min_types:
            continue
        if event.type != "GONE" and event.listing_id not in allowed_ids:
            continue
        listing = store.get_listing(event.listing_id)
        if listing is None:
            continue
        to_send.append((event, listing))

    if to_send:
        try:
            notifier.send_digest(to_send)
        except Exception:
            logger.exception("send_digest failed; events stay pending")
            raise
        store.mark_notified([e.id for e, _ in to_send])
        result.events_sent = len(to_send)

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runner.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/daftwatch/runner.py tests/test_runner.py
git commit -m "feat: run_cycle orchestration"
```

---

### Task 10: Loop, heartbeat, broken-scraper alert throttle

**Files:**
- Modify: `src/daftwatch/runner.py`
- Modify: `tests/test_runner.py`

**Interfaces:**
- Consumes: `run_cycle`, `CycleResult` from Task 9; `EmailNotifier`.
- Produces:
  - `loop(config, store, adapter, notifier, logger, *, heartbeat_path: str | None = None, sleeper=time.sleep, clock=time.monotonic, max_cycles: int | None = None) -> None`.
  - Each iteration: call `run_cycle`; touch `heartbeat_path` (write current ISO time) if set; if `result.adapter_broken`, call `notifier.send_alert("scraper may be broken", <failed search names>)` but at most once per 6 hours (tracked with `clock`); sleep `config.interval_minutes * 60` via `sleeper`. Stop after `max_cycles` iterations if set (tests), else run forever. A `run_cycle` exception is logged via `logger.exception` and the loop continues after sleeping (a transient SMTP outage must not kill the service).

- [ ] **Step 1: Write the failing tests (append to `tests/test_runner.py`)**

```python
from daftwatch.runner import loop
from daftwatch.config import NotifyConfig


class CountingNotifier(RecordingNotifier):
    def __init__(self):
        super().__init__()
        self.alerts = []
    def send_alert(self, s, b): self.alerts.append((s, b))


def test_loop_runs_n_cycles_and_heartbeats(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    hb = tmp_path / "hb"
    s = Search(name="s1", category="rent", params={})
    adapter = FakeAdapter({"s1": [mk("1", 2000)]})
    slept = []
    loop(cfg([s]), store, adapter, CountingNotifier(), logging.getLogger("t"),
         heartbeat_path=str(hb), sleeper=lambda x: slept.append(x),
         clock=lambda: 0.0, max_cycles=3)
    assert len(slept) == 3
    assert hb.exists()
    store.close()


def test_loop_throttles_broken_alerts(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="s1", category="rent", params={})
    adapter = FakeAdapter({"s1": AdapterError("boom")})
    notifier = CountingNotifier()
    t = [0.0]
    # advance 1h per cycle; 6h throttle -> alert on cycle 1 and cycle 7
    loop(cfg([s]), store, adapter, notifier, logging.getLogger("t"),
         sleeper=lambda x: t.__setitem__(0, t[0] + 3600),
         clock=lambda: t[0], max_cycles=7)
    assert len(notifier.alerts) == 2
    store.close()


def test_loop_survives_run_cycle_exception(tmp_path):
    store = Store(str(tmp_path / "t.db"))

    class ExplodingNotifier(CountingNotifier):
        def send_digest(self, items):
            raise RuntimeError("smtp down")

    s = Search(name="s1", category="rent", params={})
    adapter = FakeAdapter({"s1": [mk("1", 2000)]})
    slept = []
    loop(cfg([s]), store, adapter, ExplodingNotifier(), logging.getLogger("t"),
         sleeper=lambda x: slept.append(x), clock=lambda: 0.0, max_cycles=2)
    assert len(slept) == 2  # kept going despite the exception
    store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runner.py -v`
Expected: FAIL — `ImportError: cannot import name 'loop'`

- [ ] **Step 3: Add to `src/daftwatch/runner.py`**

```python
import time
from datetime import datetime, timezone
from pathlib import Path

_ALERT_THROTTLE_SECONDS = 6 * 3600


def loop(
    config: Config,
    store: Store,
    adapter: SearchAdapter,
    notifier: EmailNotifier,
    logger: logging.Logger,
    *,
    heartbeat_path: str | None = None,
    sleeper=time.sleep,
    clock=time.monotonic,
    max_cycles: int | None = None,
) -> None:
    last_alert: float | None = None
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            result = run_cycle(config, store, adapter, notifier, logger)
            if result.adapter_broken:
                now = clock()
                if last_alert is None or now - last_alert >= _ALERT_THROTTLE_SECONDS:
                    notifier.send_alert(
                        "scraper may be broken",
                        "Failed searches: " + ", ".join(result.searches_failed),
                    )
                    last_alert = now
        except Exception:
            logger.exception("run_cycle raised; continuing after sleep")

        if heartbeat_path:
            Path(heartbeat_path).write_text(
                datetime.now(timezone.utc).isoformat(), encoding="utf-8"
            )
        sleeper(config.interval_minutes * 60)
```

Note: move the `import time` / `from datetime ...` / `from pathlib ...` lines to the top of the file with the other imports rather than mid-file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runner.py -v`
Expected: PASS (7 tests total in the file)

- [ ] **Step 5: Commit**

```bash
git add src/daftwatch/runner.py tests/test_runner.py
git commit -m "feat: scheduling loop with heartbeat and throttled alerts"
```

---

### Task 11: CLI entry point

**Files:**
- Create: `src/daftwatch/__main__.py`
- Create: `tests/test_main.py`

**Interfaces:**
- Consumes: `load_config`, `SmtpConfig` from `daftwatch.config`; `Store`; `DaftListingsAdapter`; `EmailNotifier`; `run_cycle`, `loop`.
- Produces:
  - `build(args, env) -> tuple[Config, Store, DaftListingsAdapter, EmailNotifier, logging.Logger]` — wiring helper, testable without touching the network.
  - `main(argv: list[str] | None = None, env: Mapping | None = None) -> int`.
  - CLI: `python -m daftwatch run [--config PATH] [--db PATH]` → one `run_cycle`, print a one-line summary, exit 0. `python -m daftwatch loop [--config PATH] [--db PATH] [--heartbeat PATH]` → `loop(...)` forever. Defaults: `--config ./config.yaml`, `--db ./data/daft.db`, `--heartbeat ./data/heartbeat`.
  - `--db`'s parent directory is created if missing.
  - Logging: `logging.basicConfig(level=INFO, format="%(asctime)s %(levelname)s %(message)s")`.

- [ ] **Step 1: Write the failing test**

`tests/test_main.py`:

```python
import textwrap
from daftwatch.__main__ import main


def write_cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        interval_minutes: 1
        searches:
          - name: s1
            category: rent
            params: {}
    """))
    return p


ENV = {
    "SMTP_HOST": "h", "SMTP_PORT": "587", "SMTP_USER": "u", "SMTP_PASS": "p",
    "ALERT_FROM": "f@x.com", "ALERT_TO": "t@x.com",
}


def test_run_subcommand_creates_db_and_exits_zero(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path)
    db = tmp_path / "data" / "daft.db"

    # stub the adapter so no network call happens
    import daftwatch.__main__ as m

    class FakeAdapter:
        def __init__(self, *a, **k): pass
        def fetch(self, search): return []
    monkeypatch.setattr(m, "DaftListingsAdapter", FakeAdapter)

    rc = main(["run", "--config", str(cfg), "--db", str(db)], env=ENV)
    assert rc == 0
    assert db.exists()


def test_missing_smtp_env_returns_nonzero(tmp_path):
    cfg = write_cfg(tmp_path)
    rc = main(["run", "--config", str(cfg), "--db", str(tmp_path / "d.db")],
              env={"SMTP_HOST": "h"})
    assert rc != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'daftwatch.__main__'`

- [ ] **Step 3: Write `src/daftwatch/__main__.py`**

```python
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from daftwatch.adapter import DaftListingsAdapter
from daftwatch.config import SmtpConfig, load_config
from daftwatch.notify import EmailNotifier
from daftwatch.runner import loop, run_cycle
from daftwatch.store import Store


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="daftwatch")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "loop"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default="./config.yaml")
        sp.add_argument("--db", default="./data/daft.db")
        sp.add_argument("--heartbeat", default="./data/heartbeat")
    return p


def build(args, env: Mapping[str, str]):
    config = load_config(args.config)
    smtp = SmtpConfig.from_env(env)
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(db_path))
    adapter = DaftListingsAdapter(rate_limit_seconds=config.rate_limit_seconds)
    notifier = EmailNotifier(smtp)
    logger = logging.getLogger("daftwatch")
    return config, store, adapter, notifier, logger


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    env = os.environ if env is None else env
    args = _parser().parse_args(argv)

    try:
        config, store, adapter, notifier, logger = build(args, env)
    except KeyError as exc:
        print(f"missing required environment variable: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "run":
            result = run_cycle(config, store, adapter, notifier, logger)
            print(
                f"cycle done: {result.events_sent} sent, "
                f"{len(result.searches_failed)} search(es) failed"
            )
            return 0
        loop(
            config, store, adapter, notifier, logger,
            heartbeat_path=args.heartbeat,
        )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_main.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the whole suite**

Run: `pytest -v`
Expected: PASS (all tests from every task; ~46)

- [ ] **Step 6: Commit**

```bash
git add src/daftwatch/__main__.py tests/test_main.py
git commit -m "feat: CLI entry point"
```

---

### Task 12: Docker packaging and documentation

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`
- Create: `.env.example`
- Create: `README.md`
- Modify: `.gitignore` (ensure `data/` and `.env` present — already added in an earlier commit; verify)

**Interfaces:**
- Consumes: the finished package and `config.example.yaml`.
- Produces: a runnable container. No new code, no unit tests; verification is manual build + one live smoke run.

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml .
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

USER app
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m \
  CMD python -c "import sys,time,os; p='/data/heartbeat'; \
  sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 3600 else 1)"

ENTRYPOINT ["python", "-m", "daftwatch", "loop", \
  "--config", "/data/config.yaml", "--db", "/data/daft.db", \
  "--heartbeat", "/data/heartbeat"]
```

- [ ] **Step 2: Write `docker-compose.yml`**

```yaml
services:
  daft-watch:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/data
```

- [ ] **Step 3: Write `.dockerignore`**

```
.git
data/
.env
__pycache__/
*.pyc
.pytest_cache/
tests/
docs/
```

- [ ] **Step 4: Write `.env.example`**

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=your-16-char-app-password
ALERT_FROM=you@gmail.com
ALERT_TO=you@gmail.com
```

- [ ] **Step 5: Write `README.md`**

````markdown
# daft-watch

Personal watcher for daft.ie rental and house-share listings. Polls saved
searches on an interval, stores results in SQLite with history, and emails
you when a listing is new, drops in price, or disappears.

Personal use only. It runs at one request every ~2 seconds with an honest
user agent. Do not use it for bulk data collection or resale — daft.ie's
terms forbid that.

## Setup

1. `cp .env.example .env` and fill in your SMTP details. For Gmail, enable
   2-factor auth and create an **App Password** (Google Account → Security →
   App passwords); use that as `SMTP_PASS`.
2. `mkdir -p data && cp config.example.yaml data/config.yaml`, then edit
   `data/config.yaml`.
3. `docker compose up -d --build`
4. `docker compose logs -f` to watch it.

## Configuring searches

Each entry under `searches`:

```yaml
- name: "label for your reference"
  category: rent          # rent | sharing
  params:
    location: [dublin-city, dublin-8-dublin]   # daft.ie URL slugs
    min_beds: 1
    max_beds: 3
    min_price: 800
    max_price: 2200
```

### Finding location slugs

Do a search on daft.ie in your browser and read the URL. For
`https://www.daft.ie/property-for-rent/dublin-8-dublin` the slug is
`dublin-8-dublin`. For a whole county:
`https://www.daft.ie/property-for-rent/dublin` → `dublin`. Add as many
slugs to the `location` list as you want.

## Filters

`filters.keywords_exclude` drops any listing whose title contains one of
the given words (case-insensitive):

```yaml
filters:
  keywords_exclude: [student, "short term", "rent a room"]
```

More filter rules are planned; for now every listing that matches your
daft.ie search parameters is considered.

## Notifications

`notify.min_event_types` controls which changes trigger an email. Options:
`NEW`, `PRICE_DROP`, `PRICE_UP`, `GONE`, `BACK`. One email is sent per
cycle summarising all changes.

## Running without Docker

```bash
pip install -r requirements.txt
pip install --no-deps .
export $(grep -v '^#' .env | xargs)
python -m daftwatch run --config config.yaml --db data/daft.db      # one cycle
python -m daftwatch loop --config config.yaml --db data/daft.db     # forever
```

## Development

```bash
pip install -r requirements.txt
pytest -v
pytest -v -m live      # optional: hits the real site
```

## If it stops finding listings

daft.ie changes its site and the `daftlistings` library may lag. Only
`src/daftwatch/adapter.py` (`_default_client`) knows the library's API —
that is the single place to fix. You will also get a "scraper may be
broken" email (at most once per 6 hours) when a search errors.
````

- [ ] **Step 6: Verify the build**

Run: `docker compose build`
Expected: build succeeds.

Run: `docker compose run --rm daft-watch python -m daftwatch run --config /data/config.yaml --db /data/daft.db`
(with `data/config.yaml` and `.env` in place)
Expected: exits 0, prints a cycle summary line, `data/daft.db` created. If SMTP is real and listings match, an email arrives.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile docker-compose.yml .dockerignore .env.example README.md .gitignore
git commit -m "feat: Docker packaging and README"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `SearchAdapter` interface + `DaftListingsAdapter` | 6, 7 |
| `Listing` model, monthly price normalization | 2 |
| SQLite schema, upsert, diff (NEW/PRICE/BACK) | 3 |
| GONE after N cycles, `missing_cycles` | 4 |
| `notified` idempotency | 4, 9 |
| `filters` module (pass-through + keyword exclude) | 5 |
| `EmailNotifier` digest + STARTTLS | 8 |
| "scraper broken" email, throttled 6h | 10 |
| Per-search failure isolation | 9 |
| 429/403 backoff | 7 |
| Rate limit + jitter + honest UA | 7 (UA note: see below) |
| Config YAML, env-only credentials | 1 |
| CLI `run` / `loop` | 11 |
| Docker, compose, volume, healthcheck | 12 |
| README with slug instructions | 12 |
| Deferred filters (module exists, rules later) | 5 |

**Gap found and closed:** honest user agent. `daftlistings` manages its own
HTTP session and does not expose a UA setter in a stable way. Add to Task 7
Step 3, inside `_default_client`, after `Daft()` construction, a best-effort:

```python
try:
    self._daft._session.headers.update(
        {"User-Agent": "daft-watch/0.1 (personal listings watcher)"}
    )
except Exception:
    pass
```

Include this in the implementation of `_default_client`; it is best-effort
because the library's internals are not a stable API.

**Placeholder scan:** none — every code step has real code. The two
"replace after first real run" notes (fixture in Task 6, `daftlistings`
version in Task 1) are deliberate calibration steps with concrete fallback
instructions, not deferred work.

**Type consistency:** `Event` is `(id, listing_id, type, old_price,
new_price)` everywhere. `Listing` field list is identical in Tasks 2, 3, 6,
8, 9. `run_cycle` signature matches between Tasks 9 and 10 and 11.
`Store` methods (`begin_cycle`, `sync`, `finish_cycle`, `get_listing`,
`pending_events`, `mark_notified`, `seen_this_cycle`, `close`) are
consistent across Tasks 3, 4, 9, 10.

**Scope:** one deployable service, one plan. No decomposition needed.
