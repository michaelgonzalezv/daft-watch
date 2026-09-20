import gzip
import json
from datetime import datetime, timedelta, timezone

from daftwatch.archive import _quarter, archive_closed, build_record
from daftwatch.models import Listing
from daftwatch.store import Store


def mk(id, price=700):
    return Listing(
        id=id, category="sharing", title=f"Room {id}", url=f"https://x/{id}",
        price_eur=price, beds=None, baths=None, property_type="House",
        area="Rathmines", county="Dublin", lat=53.3, lng=-6.2, raw={"payload": "x" * 50},
        price_native=price, city="dublin", agent_phone="+353111", agent_name="Ann",
        description="Bright double room", bathroom_type="Shared", sharing_with=2,
    )


def close(store, ids, days_ago):
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    before = (datetime.now(timezone.utc) - timedelta(days=days_ago + 1)).isoformat()
    for i in ids:
        store._db.execute("UPDATE listings SET active=0, last_seen=? WHERE id=?", (when, i))
        store._record_event(i, "GONE", 700, None)
        store._db.execute("UPDATE events SET detected_at=? WHERE listing_id=? AND type='GONE'", (when, i))
        # everything that happened BEFORE it closed has to sort before it
        store._db.execute("UPDATE events SET detected_at=? WHERE listing_id=? AND type!='GONE'", (before, i))
    store._db.commit()


def read_all(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def test_archives_a_closed_listing_with_its_features_and_price_path(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.begin_cycle()
    store.sync("s", [mk("a", 700)])
    store.begin_cycle()
    store.sync("s", [mk("a", 650)])
    close(store, ["a"], days_ago=45)

    assert archive_closed(store, str(tmp_path / "arch"), after_days=30) == 1

    (f,) = list((tmp_path / "arch").iterdir())
    (rec,) = read_all(f)
    assert rec["id"] == "a" and rec["city"] == "dublin" and rec["price_native"] == 650
    assert rec["property_type"] == "House" and rec["bathroom_type"] == "Shared"
    assert rec["sharing_with"] == 2 and rec["description"] == "Bright double room"
    assert [e["type"] for e in rec["events"]] == ["NEW", "PRICE_DROP", "GONE"]
    assert rec["closed_at"] == rec["events"][-1]["at"]
    store.close()


def test_agent_contact_details_are_never_archived(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.begin_cycle()
    store.sync("s", [mk("a")])
    store._db.execute("UPDATE listings SET agent_phone='+353111', agent_name='Ann'")
    close(store, ["a"], days_ago=45)
    archive_closed(store, str(tmp_path / "arch"), after_days=30)
    (f,) = list((tmp_path / "arch").iterdir())
    text = gzip.open(f, "rt", encoding="utf-8").read()
    assert "agent" not in text and "+353111" not in text and "Ann" not in text
    store.close()


def test_a_second_run_appends_only_new_ones_never_rewrites_or_duplicates(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    d = str(tmp_path / "arch")
    store.begin_cycle()
    store.sync("s", [mk("a"), mk("b")])
    close(store, ["a"], days_ago=45)
    assert archive_closed(store, d, after_days=30) == 1
    assert archive_closed(store, d, after_days=30) == 0           # nothing new -> nothing written

    close(store, ["b"], days_ago=45)
    assert archive_closed(store, d, after_days=30) == 1
    (f,) = list((tmp_path / "arch").iterdir())
    assert [r["id"] for r in read_all(f)] == ["a", "b"]           # appended in order, no dupes
    store.close()


def test_recently_closed_and_live_listings_are_left_alone(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.begin_cycle()
    store.sync("s", [mk("live"), mk("fresh")])
    close(store, ["fresh"], days_ago=5)
    assert archive_closed(store, str(tmp_path / "arch"), after_days=30) == 0
    assert not (tmp_path / "arch").exists()                        # no empty file/dir either
    store.close()


def test_raw_payload_is_blanked_once_archived(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.begin_cycle()
    store.sync("s", [mk("a")])
    assert "payload" in store._db.execute("SELECT raw_json FROM listings").fetchone()[0]
    close(store, ["a"], days_ago=45)
    archive_closed(store, str(tmp_path / "arch"), after_days=30)
    assert store._db.execute("SELECT raw_json FROM listings").fetchone()[0] == "{}"
    store.close()


def test_files_are_split_per_quarter_of_the_close_date(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.begin_cycle()
    store.sync("s", [mk("q1"), mk("q3")])
    for i, iso in (("q1", "2026-02-10T00:00:00+00:00"), ("q3", "2026-08-10T00:00:00+00:00")):
        store._db.execute("UPDATE listings SET active=0, last_seen=? WHERE id=?", (iso, i))
        store._record_event(i, "GONE", 700, None)
        store._db.execute("UPDATE events SET detected_at=? WHERE listing_id=? AND type='GONE'", (iso, i))
    store._db.commit()
    archive_closed(store, str(tmp_path / "arch"), after_days=30)
    assert sorted(p.name for p in (tmp_path / "arch").iterdir()) == ["2026-Q1.jsonl.gz", "2026-Q3.jsonl.gz"]
    store.close()


def test_quarter_helper():
    assert _quarter("2026-01-01T00:00:00+00:00") == "2026-Q1"
    assert _quarter("2026-12-31T23:59:00+00:00") == "2026-Q4"
    assert _quarter(None) == "unknown"
    assert _quarter("garbage") == "unknown"


def test_build_record_falls_back_to_last_seen_without_a_gone_event():
    row = {"id": "x", "last_seen": "2026-09-01T00:00:00+00:00", "events": [], "distances_km": {"centre": 2.5}}
    rec = build_record(row)
    assert rec["closed_at"] == "2026-09-01T00:00:00+00:00"
    assert rec["distance_centre_km"] == 2.5
