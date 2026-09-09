import json
import subprocess
from pathlib import Path

import pytest

from daftwatch.export import to_record, write_json, git_publish
from daftwatch.models import Listing


RECORD_KEYS = [
    "id", "source", "currency", "url", "title", "price_eur", "price_native",
    "price_weekly", "beds", "room_type", "sharing_with", "rooms_available",
    "preferences", "owner_occupied", "available_from", "bathroom_type",
    "property_type", "city", "area", "lat", "lng", "distance_centre_km",
    "first_published", "last_updated", "description",
]


def mk(id="1", price_eur=700, first_published="2026-09-01", **kw):
    base = dict(
        id=id, category="sharing", title="A room", url="https://x/y",
        price_eur=price_eur, beds=None, baths=None, property_type="Apartment",
        area="Rathmines", county="Dublin", lat=53.32, lng=-6.26, raw={},
        source="daft", currency="EUR", price_native=price_eur,
        first_published=first_published,
    )
    base.update(kw)
    return Listing(**base)


# -- to_record ----------------------------------------------------------------

def test_to_record_exact_keys():
    rec = to_record(mk(distances_km={"centre": 1.5}))
    assert list(rec.keys()) == RECORD_KEYS
    assert len(rec) == 25


def test_to_record_distance_from_centre():
    rec = to_record(mk(distances_km={"centre": 2.34}))
    assert rec["distance_centre_km"] == 2.34


def test_to_record_none_distance_key_present():
    rec = to_record(mk(distances_km={}))
    assert "distance_centre_km" in rec
    assert rec["distance_centre_km"] is None


def test_to_record_owner_occupied_and_description_passthrough():
    rec = to_record(mk(owner_occupied=False, description=None))
    assert rec["owner_occupied"] is False
    assert rec["description"] is None
    rec2 = to_record(mk(owner_occupied=True, description="hi"))
    assert rec2["owner_occupied"] is True
    assert rec2["description"] == "hi"


def test_to_record_values_match_listing():
    l = mk(id="abc", price_weekly=160, room_type="Single Room",
           sharing_with=3, rooms_available=1, preferences="Female",
           available_from="2026-10-01", bathroom_type="Ensuite",
           city="dublin", last_updated="2026-09-08")
    rec = to_record(l)
    assert rec["id"] == "abc"
    assert rec["price_weekly"] == 160
    assert rec["room_type"] == "Single Room"
    assert rec["sharing_with"] == 3
    assert rec["city"] == "dublin"
    assert rec["property_type"] == "Apartment"
    assert rec["lat"] == 53.32


# -- write_json -------------------------------------------------------------

def test_write_json_valid_and_count(tmp_path):
    p = tmp_path / "listings.json"
    ls = [mk("1"), mk("2"), mk("3")]
    write_json(str(p), ls, "2026-09-09T10:00:00")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["generated_at"] == "2026-09-09T10:00:00"
    assert data["count"] == 3
    assert len(data["listings"]) == 3
    assert not (tmp_path / "listings.json.tmp").exists()


def test_write_json_dates_are_iso_strings(tmp_path):
    p = tmp_path / "listings.json"
    write_json(str(p), [mk("1", first_published="2026-09-01",
                            last_updated="2026-09-05")], "2026-09-09")
    rec = json.loads(p.read_text(encoding="utf-8"))["listings"][0]
    assert rec["first_published"] == "2026-09-01"
    assert rec["last_updated"] == "2026-09-05"


def test_write_json_sorted_price_then_newer_first(tmp_path):
    p = tmp_path / "listings.json"
    ls = [
        mk("a", price_eur=700, first_published="2026-09-01"),
        mk("b", price_eur=700, first_published="2026-09-05"),
        mk("c", price_eur=600, first_published="2026-09-02"),
    ]
    write_json(str(p), ls, "2026-09-09")
    order = [r["id"] for r in json.loads(p.read_text(encoding="utf-8"))["listings"]]
    assert order == ["c", "b", "a"]


def test_write_json_creates_parent_dirs(tmp_path):
    p = tmp_path / "deep" / "nested" / "listings.json"
    write_json(str(p), [mk("1")], "2026-09-09")
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8"))["count"] == 1


def test_write_json_none_first_published_sorts_last(tmp_path):
    p = tmp_path / "listings.json"
    ls = [
        mk("none", price_eur=700, first_published=None),
        mk("dated", price_eur=700, first_published="2026-09-01"),
    ]
    write_json(str(p), ls, "2026-09-09")
    order = [r["id"] for r in json.loads(p.read_text(encoding="utf-8"))["listings"]]
    assert order == ["dated", "none"]


# -- git_publish -----------------------------------------------------------

def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path,
                   check=True, capture_output=True)
    (path / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True,
                   capture_output=True)


def test_git_publish_commits_and_skips_unchanged(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "f.json").write_text('{"a":1}')
    assert git_publish(str(tmp_path), "f.json", "msg", push=False) is True
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                         capture_output=True, text=True).stdout
    assert "msg" in log

    # same content -> no commit
    assert git_publish(str(tmp_path), "f.json", "msg2", push=False) is False
    count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=tmp_path,
                           capture_output=True, text=True).stdout.strip()
    assert count == "2"

    # changed content -> commits again
    (tmp_path / "f.json").write_text('{"a":2}')
    assert git_publish(str(tmp_path), "f.json", "msg3", push=False) is True
    count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=tmp_path,
                           capture_output=True, text=True).stdout.strip()
    assert count == "3"


def test_git_publish_bogus_repo_returns_false(tmp_path, caplog):
    missing = tmp_path / "nope"
    with caplog.at_level("ERROR"):
        assert git_publish(str(missing), "f.json", "msg", push=False) is False
    assert any("git_publish failed" in r.message for r in caplog.records)


def test_git_publish_push_no_remote_returns_false_but_commit_stands(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "f.json").write_text('{"a":1}')
    assert git_publish(str(tmp_path), "f.json", "committed-msg", push=True) is False
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                         capture_output=True, text=True).stdout
    assert "committed-msg" in log
