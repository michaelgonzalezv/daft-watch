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
