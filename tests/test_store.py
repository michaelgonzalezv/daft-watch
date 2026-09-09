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


def test_schema_stamps_user_version(store):
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 1


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
