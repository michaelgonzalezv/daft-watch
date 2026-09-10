import sqlite3

import pytest
from daftwatch.models import Listing
from daftwatch.store import Store


def mk(id="1", price=1000, category="rent"):
    return Listing(
        id=id, category=category, title=f"Flat {id}", url=f"https://daft.ie/{id}",
        price_eur=price, beds=2, baths=1, property_type="Apartment",
        area="D8", county="Dublin", lat=53.3, lng=-6.2, raw={"x": 1},
    )


def mk_share(id, price):
    return Listing(
        id=id, category="sharing", title=f"Room {id}", url=f"https://daft.ie/{id}",
        price_eur=price, beds=None, baths=None, property_type="Apartment",
        area=None, county="Dublin", lat=53.3, lng=-6.2, raw={}, price_native=price,
    )


def _detail_fields(**over):
    base = dict(
        sharing_with=None, rooms_available=None, preferences=None,
        owner_occupied=None, available_from=None, bathroom_type=None,
        description=None, last_updated=None,
    )
    base.update(over)
    return base


NEW_COLS = {
    "source", "currency", "price_native", "price_weekly", "first_published",
    "last_updated", "sharing_with", "rooms_available", "preferences",
    "owner_occupied", "available_from", "bathroom_type", "description",
    "room_type", "city", "detail_json", "detail_fetched", "previous_price",
}

V1_SCHEMA = """
CREATE TABLE listings (
    id TEXT PRIMARY KEY, category TEXT, title TEXT, url TEXT,
    price_eur INTEGER, beds INTEGER, baths INTEGER, property_type TEXT,
    area TEXT, county TEXT, lat REAL, lng REAL, raw_json TEXT,
    first_seen TEXT, last_seen TEXT,
    active INTEGER NOT NULL DEFAULT 1, missing_cycles INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT, type TEXT,
    old_price INTEGER, new_price INTEGER, detected_at TEXT
);
CREATE TABLE notified (event_id INTEGER PRIMARY KEY, sent_at TEXT);
PRAGMA user_version = 1;
"""


def _cols(store):
    return {r[1] for r in store._db.execute("PRAGMA table_info(listings)")}


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def test_schema_stamps_user_version(store):
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 4


def test_fresh_db_is_v2_with_new_columns(store):
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert NEW_COLS <= _cols(store)


def test_v1_db_migrates_preserving_rows(tmp_path):
    p = str(tmp_path / "v1.db")
    db = sqlite3.connect(p)
    db.executescript(V1_SCHEMA)
    db.execute(
        "INSERT INTO listings (id, category, title, url, price_eur, beds, "
        "first_seen, last_seen) VALUES "
        "('old1','rent','Old Flat','https://daft.ie/old1', 950, 2, 't0','t0')"
    )
    db.commit()
    db.close()

    s = Store(p)
    try:
        assert s._db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert NEW_COLS <= _cols(s)
        row = s._db.execute("SELECT * FROM listings WHERE id='old1'").fetchone()
        assert row["price_eur"] == 950
        assert row["title"] == "Old Flat"
        assert row["sharing_with"] is None
        assert row["detail_json"] is None
        assert row["detail_fetched"] == 0
    finally:
        s.close()


def test_migration_idempotent_on_reopen(tmp_path):
    p = str(tmp_path / "v1.db")
    db = sqlite3.connect(p)
    db.executescript(V1_SCHEMA)
    db.commit()
    db.close()
    Store(p).close()
    s = Store(p)
    try:
        assert s._db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert NEW_COLS <= _cols(s)
    finally:
        s.close()


def test_needs_detail_price_cap_and_limit(store):
    store.begin_cycle()
    store.sync("s", [mk_share("a800", 800), mk_share("b1200", 1200),
                     mk_share("c750", 750)])
    assert set(store.needs_detail(800)) == {"a800", "c750"}

    store.apply_detail("a800", _detail_fields(sharing_with=3))
    assert set(store.needs_detail(800)) == {"c750"}

    store.begin_cycle()
    store.sync("s", [mk_share("d100", 100), mk_share("e200", 200)])
    assert len(store.needs_detail(800, limit=1)) == 1
    assert len(store.needs_detail(800)) == 3


def test_needs_detail_excludes_zero_price_and_inactive(store):
    store.begin_cycle()
    store.sync("s", [mk_share("free", 0), mk_share("live", 500)])
    store._db.execute("UPDATE listings SET active=0 WHERE id='live'")
    store._db.commit()
    assert store.needs_detail(800) == []


def test_apply_detail_roundtrips_via_get_listing(store):
    store.begin_cycle()
    store.sync("s", [mk_share("x", 700)])
    store.apply_detail("x", _detail_fields(
        sharing_with=4, preferences="Female", owner_occupied=False,
        rooms_available=1, description="a nice room", last_updated="2026-09-08",
    ))
    got = store.get_listing("x")
    assert got.sharing_with == 4
    assert got.preferences == "Female"
    assert got.owner_occupied is False
    assert got.rooms_available == 1
    assert got.last_updated == "2026-09-08"
    assert got.detail_fetched is True

    store.apply_detail("x", _detail_fields(owner_occupied=True))
    assert store.get_listing("x").owner_occupied is True


def test_set_distances_merges_into_detail_json(store):
    store.begin_cycle()
    store.sync("s", [mk_share("x", 700)])
    store.set_distances("x", {"centre": 3.4})
    assert store.get_listing("x").distances_km == {"centre": 3.4}
    store.set_distances("x", {"work": 5.1})
    assert store.get_listing("x").distances_km == {"centre": 3.4, "work": 5.1}


def test_set_city(store):
    store.begin_cycle()
    store.sync("s", [mk_share("x", 700)])
    store.set_city("x", "dublin")
    assert store.get_listing("x").city == "dublin"
    store.set_city("x", None)
    assert store.get_listing("x").city is None


def test_active_listings_only_active_and_hydrated(store):
    store.begin_cycle()
    store.sync("s", [mk_share("x", 700), mk_share("y", 650)])
    store.apply_detail("x", _detail_fields(sharing_with=2, description="nice"))
    store._db.execute("UPDATE listings SET active=0 WHERE id='y'")
    store._db.commit()
    act = store.active_listings()
    assert {l.id for l in act} == {"x"}
    assert act[0].sharing_with == 2
    assert act[0].description == "nice"


def test_sync_persists_new_scalar_fields(store):
    import dataclasses
    l = dataclasses.replace(
        mk_share("x", 700), price_weekly=160, room_type="Single Room",
        first_published="2026-09-01", price_native=700,
    )
    store.begin_cycle()
    store.sync("s", [l])
    got = store.get_listing("x")
    assert got.price_weekly == 160
    assert got.room_type == "Single Room"
    assert got.first_published == "2026-09-01"
    assert got.price_native == 700
    assert got.source == "daft"
    assert got.currency == "EUR"


def test_sync_update_preserves_enrichment(store):
    store.begin_cycle()
    store.sync("s", [mk_share("x", 700)])
    store.apply_detail("x", _detail_fields(sharing_with=3))
    store.set_distances("x", {"centre": 2.0})
    store.set_city("x", "dublin")

    store.begin_cycle()
    store.sync("s", [mk_share("x", 690)])
    got = store.get_listing("x")
    assert got.sharing_with == 3
    assert got.detail_fetched is True
    assert got.distances_km == {"centre": 2.0}
    assert got.city == "dublin"
    assert got.price_eur == 690


def test_sync_records_previous_price_on_change(store):
    store.begin_cycle()
    store.sync("s", [mk_share("x", 700)])
    assert store.get_listing("x").previous_price is None

    store.begin_cycle()
    store.sync("s", [mk_share("x", 650)])          # dropped
    got = store.get_listing("x")
    assert got.price_eur == 650
    assert got.previous_price == 700

    store.begin_cycle()
    store.sync("s", [mk_share("x", 650)])          # unchanged -> holds
    assert store.get_listing("x").previous_price == 700

    store.begin_cycle()
    store.sync("s", [mk_share("x", 680)])          # up
    assert store.get_listing("x").previous_price == 650


def test_export_listings_includes_recently_gone(store):
    store.begin_cycle()
    store.sync("s", [mk_share("keep", 700), mk_share("gone", 650)])
    store.begin_cycle()
    store.sync("s", [mk_share("keep", 700)])
    store.finish_cycle(gone_after_cycles=1)          # 'gone' -> active=0 + GONE

    ex = {l.id: l for l in store.export_listings(gone_within_days=30)}
    assert set(ex) == {"keep", "gone"}
    assert ex["keep"].status == "available"
    assert ex["gone"].status == "off_market"
    assert ex["gone"].off_market_since is not None

    # a cutoff in the future -> the off-market one drops out
    assert {l.id for l in store.export_listings(gone_within_days=-1)} == {"keep"}


def test_snapshot_prices_percentiles_and_idempotent(store):
    store.begin_cycle()
    ls = [mk_share(str(i), p) for i, p in enumerate([400, 500, 600, 700, 800])]
    store.sync("s", ls)
    for l in ls:
        store.set_city(l.id, "cork")
    store.snapshot_prices("2026-09-10")
    rows = store.price_history_rows(days=400)
    assert len(rows) == 1
    r = rows[0]
    assert (r["date"], r["city"], r["category"], r["count"]) == ("2026-09-10", "cork", "sharing", 5)
    assert r["median"] == 600 and r["p25"] == 500 and r["p75"] == 700

    # re-run same day overwrites, does not duplicate
    store.snapshot_prices("2026-09-10")
    assert len(store.price_history_rows(days=400)) == 1


def test_dump_sql_roundtrips(store, tmp_path):
    import sqlite3 as _sq
    store.begin_cycle()
    store.sync("s", [mk_share("x", 700), mk_share("y", 650)])
    p = tmp_path / "sub" / "dump.sql"
    store.dump_sql(str(p))
    assert p.exists() and "CREATE TABLE" in p.read_text(encoding="utf-8")
    restored = _sq.connect(":memory:")
    restored.executescript(p.read_text(encoding="utf-8"))
    n = restored.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    assert n == 2


def test_events_history_groups_and_orders(store):
    store.begin_cycle()
    store.sync("s", [mk_share("a", 700)])
    store.begin_cycle()
    store.sync("s", [mk_share("a", 650)])            # PRICE_DROP
    h = store.events_history(days=90)
    assert [e["type"] for e in h["a"]] == ["NEW", "PRICE_DROP"]
    assert h["a"][1]["old_price"] == 700 and h["a"][1]["new_price"] == 650
    assert store.events_history(days=-1) == {}


def test_raw_json_persisted_on_insert_and_update(store):
    import dataclasses
    import json
    store.begin_cycle()
    store.sync("s1", [mk("1", price=1000)])
    raw = store._db.execute(
        "SELECT raw_json FROM listings WHERE id=?", ("1",)
    ).fetchone()[0]
    assert json.loads(raw) == {"x": 1}
    store.begin_cycle()
    updated = dataclasses.replace(mk("1", price=1100), raw={"x": 2, "updated": True})
    store.sync("s1", [updated])
    raw2 = store._db.execute(
        "SELECT raw_json FROM listings WHERE id=?", ("1",)
    ).fetchone()[0]
    assert json.loads(raw2) == {"x": 2, "updated": True}


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


def test_back_event_when_inactive_listing_reappears(store):
    # First sync: create listing in active state
    store.begin_cycle()
    store.sync("s1", [mk("1", price=1000)])

    # Manually mark it as inactive (simulating Task 4 GONE sweep)
    store._db.execute("UPDATE listings SET active=0 WHERE id=?", ("1",))
    store._db.commit()

    # Next cycle: listing reappears
    store.begin_cycle()
    events = store.sync("s1", [mk("1", price=1000)])

    # Should emit exactly one BACK event
    assert len(events) == 1
    assert events[0].type == "BACK"
    assert events[0].listing_id == "1"
    assert events[0].old_price == 1000
    assert events[0].new_price == 1000


def test_back_event_takes_precedence_over_price_change(store):
    # First sync: create listing
    store.begin_cycle()
    store.sync("s1", [mk("1", price=1000)])

    # Mark as inactive
    store._db.execute("UPDATE listings SET active=0 WHERE id=?", ("1",))
    store._db.commit()

    # Reappear with different price: should emit BACK, not PRICE_DROP/UP
    store.begin_cycle()
    events = store.sync("s1", [mk("1", price=900)])

    # BACK should take precedence
    assert len(events) == 1
    assert events[0].type == "BACK"
    assert events[0].old_price == 1000
    assert events[0].new_price == 900


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
