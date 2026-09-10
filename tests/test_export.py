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
    assert write_json(str(p), ls, "2026-09-09T10:00:00") is True
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["generated_at"] == "2026-09-09T10:00:00"
    assert data["count"] == 3
    assert len(data["listings"]) == 3
    assert not (tmp_path / "listings.json.tmp").exists()
    assert p.read_text(encoding="utf-8").endswith("\n")


def test_write_json_skips_when_listings_unchanged(tmp_path):
    p = tmp_path / "listings.json"
    ls = [mk("1", price_eur=700), mk("2", price_eur=800)]
    assert write_json(str(p), ls, "2026-09-09T10:00:00") is True
    first = p.read_text(encoding="utf-8")

    # same listings (input order irrelevant), different generated_at -> no rewrite
    assert write_json(str(p), list(reversed(ls)), "2026-09-09T11:30:00") is False
    assert p.read_text(encoding="utf-8") == first  # generated_at untouched

    # a changed listing -> rewrites, returns True
    assert write_json(str(p), [mk("1", price_eur=650), mk("2", price_eur=800)],
                      "2026-09-09T12:00:00") is True
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["generated_at"] == "2026-09-09T12:00:00"


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


def test_git_publish_commit_is_scoped_to_the_pathspec(tmp_path):
    _init_repo(tmp_path)
    # an unrelated staged change the user is working on
    (tmp_path / "other.txt").write_text("user WIP")
    subprocess.run(["git", "add", "other.txt"], cwd=tmp_path, check=True,
                   capture_output=True)

    (tmp_path / "f.json").write_text('{"a":1}')
    assert git_publish(str(tmp_path), "f.json", "data msg", push=False) is True

    names = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True,
    ).stdout.split()
    assert names == ["f.json"]  # other.txt NOT swept into the data commit

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path, capture_output=True, text=True,
    ).stdout.split()
    assert "other.txt" in staged  # still staged, untouched


def test_git_publish_pulls_before_commit(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True,
                   capture_output=True)

    def _clone(name):
        d = tmp_path / name
        subprocess.run(["git", "clone", str(origin), str(d)], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t.t"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(d), "config", "user.name", "T"],
                       check=True, capture_output=True)
        return d

    a = _clone("a")
    (a / "seed.txt").write_text("seed")
    subprocess.run(["git", "-C", str(a), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(a), "commit", "-m", "init"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(a), "push", "-u", "origin", "HEAD"],
                   check=True, capture_output=True)

    b = _clone("b")
    (b / "upstream.txt").write_text("ahead")
    subprocess.run(["git", "-C", str(b), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(b), "commit", "-m", "upstream move"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(b), "push"], check=True, capture_output=True)

    # a is now behind origin; git_publish must pull --rebase then commit + push
    (a / "f.json").write_text('{"a":1}')
    assert git_publish(str(a), "f.json", "data msg", push=True) is True

    log = subprocess.run(["git", "-C", str(a), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "upstream move" in log  # pull --rebase folded the remote commit in
    names = subprocess.run(
        ["git", "-C", str(a), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True,
    ).stdout.split()
    assert names == ["f.json"]


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
