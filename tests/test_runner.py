import json
import logging
import subprocess

import pytest

from daftwatch.adapter import AdapterError, SearchAdapter
from daftwatch.config import Config, NotifyConfig, PublishConfig, Search
from daftwatch.models import Listing
from daftwatch.store import Store
from daftwatch.runner import export_and_publish, run_cycle
import daftwatch.runner as runner_mod

# Cork / Dublin city centres (mirror daftwatch.geo.CENTRES)
_CORK = (51.8979, -8.4706)
_DUBLIN = (53.3473, -6.2591)


@pytest.fixture(autouse=True)
def _no_real_fx_network_calls(monkeypatch):
    # run_cycle resolves fetch_rates_usd off daftwatch.runner's globals at
    # call time (not a bound default), specifically so this one patch covers
    # every publish-exercising test below without threading a fake through
    # each call site.
    monkeypatch.setattr(
        runner_mod, "fetch_rates_usd", lambda currencies: {c: 1.0 for c in currencies}
    )


def mk(id, price, title="Flat"):
    return Listing(id=id, category="rent", title=title, url=f"https://d/{id}",
                   price_eur=price, beds=2, baths=1, property_type="Apartment",
                   area="D8", county="Dublin", lat=None, lng=None, raw={})


def mkshare(id, price, lat=_CORK[0], lng=_CORK[1], title="Share"):
    return Listing(id=id, category="sharing", title=title,
                   url=f"https://www.daft.ie/share/{id}",
                   price_eur=price, beds=None, baths=1, property_type="Apartment",
                   area=None, county=None, lat=lat, lng=lng, raw={})


class FakeAdapter(SearchAdapter):
    def __init__(self, by_search, details=None):
        self.by_search = by_search
        self.details = details or {}
        self.detail_calls = []
        self.closed = 0

    def fetch(self, search):
        v = self.by_search[search.name]
        if isinstance(v, Exception):
            raise v
        return v

    def detail(self, path):
        self.detail_calls.append(path)
        v = self.details.get(path, {"_overview": {}})
        if isinstance(v, Exception):
            raise v
        return v

    def close(self):
        self.closed += 1


class RecordingNotifier:
    def __init__(self): self.digests = []; self.watchlists = []
    def send_digest(self, items, watchlist_items=None):
        self.digests.append(list(items))
        self.watchlists.append(list(watchlist_items or []))
    def send_alert(self, s, b): pass


def cfg(searches, min_types=("NEW", "PRICE_DROP", "GONE"), filters=None,
        publish=None, email_distance_km=None, detail_price_cap=800,
        email_max_price=None):
    return Config(searches=list(searches), gone_after_cycles=1,
                  filters=filters or {},
                  notify=NotifyConfig(min_event_types=list(min_types)),
                  detail_price_cap=detail_price_cap,
                  publish=publish,
                  email_distance_km=email_distance_km or {},
                  email_max_price=email_max_price)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path,
                   check=True, capture_output=True)
    (path / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True,
                   capture_output=True)


def _pub(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    return PublishConfig(json_path=str(repo / "listings.json"), repo_dir=str(repo),
                         file_rel="listings.json", git_push=False), repo


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


class BrokenNotifier:
    def send_digest(self, items, watchlist_items=None): raise RuntimeError("smtp down")
    def send_alert(self, s, b): pass


def test_no_spurious_gone_on_adapter_failure(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="s1", category="rent", params={})
    notifier = RecordingNotifier()
    log = logging.getLogger("t")

    # cycle 1: listing stored active
    run_cycle(cfg([s]), store, FakeAdapter({"s1": [mk("1", 2000)]}), notifier, log)
    assert store.get_listing("1") is not None

    # cycle 2: same search fails; gone_after_cycles=1
    r = run_cycle(cfg([s]), store,
                  FakeAdapter({"s1": AdapterError("boom")}), notifier, log)
    assert r.adapter_broken is True
    sent_types = [ev.type for digest in notifier.digests for ev, _ in digest]
    assert "GONE" not in sent_types
    assert store.get_listing("1") is not None
    active = store._db.execute(
        "SELECT active FROM listings WHERE id=?", ("1",)
    ).fetchone()[0]
    assert active == 1
    store.close()


def test_send_digest_failure_keeps_events_pending(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="s1", category="rent", params={})
    log = logging.getLogger("t")

    with pytest.raises(RuntimeError):
        run_cycle(cfg([s]), store, FakeAdapter({"s1": [mk("1", 2000)]}),
                  BrokenNotifier(), log)
    assert store.pending_events()  # not marked notified

    notifier = RecordingNotifier()
    r = run_cycle(cfg([s]), store, FakeAdapter({"s1": [mk("1", 2000)]}),
                  notifier, log)
    assert r.events_sent == 1
    assert len(notifier.digests[0]) == 1
    store.close()


def test_gone_event_bypasses_filter(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="s1", category="rent", params={})
    notifier = RecordingNotifier()
    log = logging.getLogger("t")

    # cycle 1: listing present
    run_cycle(cfg([s]), store, FakeAdapter({"s1": [mk("1", 2000, "Student flat")]}),
              notifier, log)

    # cycle 2: absent from all searches, filter would exclude everything
    r = run_cycle(cfg([s], min_types=("GONE",),
                      filters={"keywords_exclude": ["student"]}),
                  store, FakeAdapter({"s1": []}), notifier, log)
    assert r.events_sent == 1
    assert notifier.digests[-1][0][0].type == "GONE"
    store.close()


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
        def send_digest(self, items, watchlist_items=None):
            raise RuntimeError("smtp down")

    s = Search(name="s1", category="rent", params={})
    adapter = FakeAdapter({"s1": [mk("1", 2000)]})
    slept = []
    loop(cfg([s]), store, adapter, ExplodingNotifier(), logging.getLogger("t"),
         sleeper=lambda x: slept.append(x), clock=lambda: 0.0, max_cycles=2)
    assert len(slept) == 2  # kept going despite the exception
    store.close()


# -- Task 9: detail loop, distances, export, curated digest ----------------

def test_full_cycle_detail_and_export(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    s = Search(name="Cork sharing <=800", category="sharing", params={})
    adapter = FakeAdapter(
        {"Cork sharing <=800": [mkshare("1", 700), mkshare("2", 900)]},
        details={"/share/1": {"_overview": {"sharing with": "3",
                                            "bedrooms available": "1"},
                              "description": "Bright room"}},
    )
    notifier = RecordingNotifier()
    r = run_cycle(cfg([s], publish=pub), store, adapter, notifier,
                  logging.getLogger("t"))

    # only the sub-cap listing gets a detail fetch
    assert adapter.detail_calls == ["/share/1"]
    got = store.get_listing("1")
    assert got.sharing_with == 3
    assert got.city == "cork"
    assert got.distances_km["centre"] < 1.0  # sitting on the centre

    # listings.json written + committed
    data = json.loads((repo / "listings.json").read_text(encoding="utf-8"))
    assert {rec["id"] for rec in data["listings"]} == {"1", "2"}
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         capture_output=True, text=True).stdout
    assert "data: rentals listings" in log

    assert r.events_sent == 2
    store.close()


def test_watchlist_events_bypass_the_email_filters(tmp_path, monkeypatch):
    monkeypatch.setattr("daftwatch.runner.fetch_watchlist", lambda a, k: {"w1"})
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="Dublin sharing", category="sharing", params={})
    far = mkshare("w1", 700, lat=_DUBLIN[0] + 0.2, lng=_DUBLIN[1])   # ~22 km out
    near = mkshare("n1", 700, lat=_DUBLIN[0], lng=_DUBLIN[1])
    adapter = FakeAdapter({"Dublin sharing": [far, near]})
    notifier = RecordingNotifier()
    run_cycle(cfg([s], email_distance_km={"dublin": 6}),
              store, adapter, notifier, logging.getLogger("t"))
    assert {l.id for _, l in notifier.digests[0]} == {"n1"}      # distance-filtered
    assert {l.id for _, l in notifier.watchlists[0]} == {"w1"}   # kept anyway
    store.close()


def test_email_max_price_narrows_digest_not_export(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    s = Search(name="Dublin sharing", category="sharing", params={})
    cheap = mkshare("c", 700, lat=_DUBLIN[0], lng=_DUBLIN[1])
    pricey = mkshare("p", 1500, lat=_DUBLIN[0], lng=_DUBLIN[1])
    adapter = FakeAdapter({"Dublin sharing": [cheap, pricey]})
    notifier = RecordingNotifier()
    r = run_cycle(cfg([s], publish=pub, email_max_price=800,
                      email_distance_km={"dublin": 6}),
                  store, adapter, notifier, logging.getLogger("t"))

    data = json.loads((repo / "listings.json").read_text(encoding="utf-8"))
    assert {rec["id"] for rec in data["listings"]} == {"c", "p"}  # both exported
    assert r.events_sent == 1                                     # only €700 emailed
    assert {l.id for _, l in notifier.digests[0]} == {"c"}
    store.close()


def test_non_eur_listings_excluded_from_digest_entirely(tmp_path):
    # Regression: the first real cycle with Kijiji's 19 Canadian regions sent
    # a single ~700-row digest because every new CAD listing was "NEW" and
    # nothing capped it (email_max_price is authored in EUR — comparing it
    # against CAD would have been wrong the other way). Non-EUR listings must
    # not reach the digest until they have their own configured threshold.
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    cad_room = Listing(
        id="kj1", category="sharing", title="Room", url="https://kijiji.ca/1",
        price_eur=800, beds=None, baths=None, property_type="Room",
        area=None, county=None, lat=_DUBLIN[0], lng=_DUBLIN[1], raw={},
        source="kijiji", currency="CAD", country="Canada", price_native=800,
    )
    s = Search(name="Toronto sharing", category="sharing", params={}, source="kijiji")
    adapter = FakeAdapter({"Toronto sharing": [cad_room]})
    notifier = RecordingNotifier()
    r = run_cycle(cfg([s], publish=pub), store, adapter, notifier, logging.getLogger("t"))

    data = json.loads((repo / "listings.json").read_text(encoding="utf-8"))
    assert {rec["id"] for rec in data["listings"]} == {"kj1"}  # still exported
    assert r.events_sent == 0                                  # but not emailed
    store.close()


def test_publish_writes_compare_json_when_configured(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    pub = PublishConfig(
        json_path=str(repo / "listings.json"), repo_dir=str(repo),
        file_rel="listings.json", git_push=False,
        compare_path=str(repo / "compare.json"), compare_rel="compare.json",
    )
    rooms = [mkshare(f"d{i}", 800) for i in range(20)]
    s = Search(name="Cork sharing", category="sharing", params={})
    adapter = FakeAdapter({"Cork sharing": rooms})
    run_cycle(cfg([s], publish=pub), store, adapter, RecordingNotifier(),
              logging.getLogger("t"))

    data = json.loads((repo / "compare.json").read_text(encoding="utf-8"))
    assert "generated_at" in data
    assert "caveats" in data and len(data["caveats"]) > 0
    store.close()


def test_publish_skips_compare_json_when_not_configured(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)  # no compare_path/compare_rel
    s = Search(name="Cork sharing", category="sharing", params={})
    adapter = FakeAdapter({"Cork sharing": [mkshare("d1", 800)]})
    run_cycle(cfg([s], publish=pub), store, adapter, RecordingNotifier(),
              logging.getLogger("t"))
    assert not (repo / "compare.json").exists()
    store.close()


def test_publish_embeds_fx_usd_for_every_currency_present(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    eur_room = mkshare("d1", 700)
    cad_room = Listing(
        id="kj1", category="sharing", title="Room", url="https://kijiji.ca/1",
        price_eur=800, beds=None, baths=None, property_type="Room",
        area=None, county=None, lat=_DUBLIN[0], lng=_DUBLIN[1], raw={},
        source="kijiji", currency="CAD", country="Canada", price_native=800,
    )
    s1 = Search(name="Cork sharing", category="sharing", params={})
    s2 = Search(name="Toronto sharing", category="sharing", params={}, source="kijiji")
    adapter = FakeAdapter({"Cork sharing": [eur_room], "Toronto sharing": [cad_room]})
    seen = []

    def fake_fx(currencies):
        seen.append(sorted(currencies))
        return {c: 42.0 for c in currencies}

    run_cycle(cfg([s1, s2], publish=pub), store, adapter, RecordingNotifier(),
              logging.getLogger("t"), fx_fetcher=fake_fx)

    data = json.loads((repo / "listings.json").read_text(encoding="utf-8"))
    assert data["fx_usd"] == {"CAD": 42.0, "EUR": 42.0}
    assert seen == [["CAD", "EUR"]]  # only currencies actually present, sorted
    store.close()


def test_digest_distance_filtered_but_still_exported(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    far = mkshare("d1", 700, lat=_DUBLIN[0] + 0.072, lng=_DUBLIN[1])  # ~8 km N
    s = Search(name="Dublin sharing", category="sharing", params={})
    adapter = FakeAdapter({"Dublin sharing": [far]})
    notifier = RecordingNotifier()
    r = run_cycle(cfg([s], publish=pub, email_distance_km={"dublin": 6}),
                  store, adapter, notifier, logging.getLogger("t"))

    data = json.loads((repo / "listings.json").read_text(encoding="utf-8"))
    assert [rec["id"] for rec in data["listings"]] == ["d1"]  # exported
    assert notifier.digests == []  # but not emailed
    assert r.events_sent == 0
    store.close()


def test_digest_sorted_price_ascending(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="Cork sharing", category="sharing", params={})
    adapter = FakeAdapter({"Cork sharing": [mkshare("a", 900), mkshare("b", 600),
                                            mkshare("c", 750)]})
    notifier = RecordingNotifier()
    run_cycle(cfg([s]), store, adapter, notifier, logging.getLogger("t"))
    prices = [l.price_eur for _, l in notifier.digests[0]]
    assert prices == [600, 750, 900]
    store.close()


def test_publish_none_skips_export_but_emails(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="s1", category="rent", params={})
    adapter = FakeAdapter({"s1": [mk("1", 2000)]})
    notifier = RecordingNotifier()
    r = run_cycle(cfg([s]), store, adapter, notifier, logging.getLogger("t"))
    assert r.events_sent == 1
    assert not (tmp_path / "repo").exists()
    store.close()


def test_detail_adapter_error_is_isolated(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="Cork sharing", category="sharing", params={})
    adapter = FakeAdapter(
        {"Cork sharing": [mkshare("1", 700), mkshare("2", 750)]},
        details={"/share/1": AdapterError("boom"),
                 "/share/2": {"_overview": {"sharing with": "2"}}},
    )
    notifier = RecordingNotifier()
    r = run_cycle(cfg([s]), store, adapter, notifier, logging.getLogger("t"))
    assert adapter.detail_calls == ["/share/1", "/share/2"]
    assert store.get_listing("2").sharing_with == 2
    assert r.events_sent == 2
    store.close()


def test_detail_none_marks_delisted_without_tripping_breaker(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="Cork sharing", category="sharing", params={})
    shares = [mkshare(str(i), 700) for i in range(5)]
    adapter = FakeAdapter(
        {"Cork sharing": shares},
        # first three were delisted between fetch and detail -> adapter.detail None
        details={f"/share/{i}": None for i in range(3)},
    )
    notifier = RecordingNotifier()
    run_cycle(cfg([s]), store, adapter, notifier, logging.getLogger("t"))
    # every candidate visited (a None is not a failure, so no 3-strike break)
    assert adapter.detail_calls == [f"/share/{i}" for i in range(5)]
    # all five marked enriched -> needs_detail is now empty (no re-fetch churn)
    assert store.needs_detail(10_000) == []
    store.close()


def test_detail_loop_circuit_breaker(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    s = Search(name="Cork sharing", category="sharing", params={})
    shares = [mkshare(str(i), 700) for i in range(10)]
    adapter = FakeAdapter(
        {"Cork sharing": shares},
        details={f"/share/{i}": AdapterError("cf block") for i in range(10)},
    )
    notifier = RecordingNotifier()
    r = run_cycle(cfg([s], publish=pub), store, adapter, notifier,
                  logging.getLogger("t"))
    assert len(adapter.detail_calls) == 3  # stopped after 3 consecutive failures
    assert (repo / "listings.json").exists()  # export still ran
    assert r.events_sent == 10  # digest still sent
    store.close()


def test_detail_loop_breaker_resets_on_success(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="Cork sharing", category="sharing", params={})
    shares = [mkshare(str(i), 700) for i in range(6)]
    # fail, fail, ok, fail, fail, fail -> breaker resets at the ok, trips on #6
    adapter = FakeAdapter(
        {"Cork sharing": shares},
        details={
            "/share/0": AdapterError("x"), "/share/1": AdapterError("x"),
            "/share/2": {"_overview": {"sharing with": "1"}},
            "/share/3": AdapterError("x"), "/share/4": AdapterError("x"),
            "/share/5": AdapterError("x"),
        },
    )
    r = run_cycle(cfg([s]), store, adapter, RecordingNotifier(),
                  logging.getLogger("t"))
    assert adapter.detail_calls == [f"/share/{i}" for i in range(6)]
    assert store.get_listing("2").sharing_with == 1
    assert r.events_sent == 6
    store.close()


def test_export_write_json_raising_does_not_kill_digest(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    monkeypatch.setattr(
        "daftwatch.runner.export.write_json",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    s = Search(name="Cork sharing", category="sharing", params={})
    adapter = FakeAdapter({"Cork sharing": [mkshare("1", 700)]})
    notifier = RecordingNotifier()
    r = run_cycle(cfg([s], publish=pub), store, adapter, notifier,
                  logging.getLogger("t"))
    assert r.events_sent == 1
    assert len(notifier.digests[0]) == 1
    store.close()


def test_detail_loop_skips_empty_url(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="Cork sharing", category="sharing", params={})
    blank = mkshare("1", 700)
    object.__setattr__(blank, "url", None)  # NULL url column round-trips as None
    adapter = FakeAdapter({"Cork sharing": [blank, mkshare("2", 700)]})
    r = run_cycle(cfg([s]), store, adapter, RecordingNotifier(),
                  logging.getLogger("t"))
    assert adapter.detail_calls == ["/share/2"]  # id 1 skipped, no crash
    assert r.events_sent == 2
    store.close()


def test_json_export_is_price_filtered(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    s = Search(name="Cork sharing", category="sharing", params={})
    # daft's rentalPrice_to filters on native weekly price -> a €350/wk share
    # (~€1517/mo) comes back; the JSON must still drop it.
    adapter = FakeAdapter({"Cork sharing": [mkshare("cheap", 700),
                                            mkshare("pricey", 1500)]})
    notifier = RecordingNotifier()
    run_cycle(cfg([s], filters={"max_price": 800}, publish=pub),
              store, adapter, notifier, logging.getLogger("t"))

    assert {l.id for l in store.active_listings()} == {"cheap", "pricey"}
    data = json.loads((repo / "listings.json").read_text(encoding="utf-8"))
    ids = {rec["id"] for rec in data["listings"]}
    assert ids == {"cheap"}  # pricey excluded from the JSON
    store.close()


def test_a_favourite_survives_max_price_and_keywords_exclude(tmp_path, monkeypatch):
    # keep_ids exempts a favourite from the store's day cutoff, but
    # export_and_publish still ran it through filters.apply() afterwards —
    # a favourite whose last known price is above max_price, or whose title
    # matches keywords_exclude, must not quietly disappear either.
    monkeypatch.setattr("daftwatch.runner.fetch_watchlist", lambda a, k: {"fav"})
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    s = Search(name="Cork sharing", category="sharing", params={})
    fav = mkshare("fav", 1500, title="student room")  # over max_price AND excluded by keyword
    plain = mkshare("plain", 1500, title="student room")
    adapter = FakeAdapter({"Cork sharing": [fav, plain]})
    run_cycle(
        cfg([s], filters={"max_price": 800, "keywords_exclude": ["student"]}, publish=pub),
        store, adapter, RecordingNotifier(), logging.getLogger("t"),
    )

    data = json.loads((repo / "listings.json").read_text(encoding="utf-8"))
    ids = {rec["id"] for rec in data["listings"]}
    assert ids == {"fav"}  # 'plain' correctly filtered out, 'fav' bypasses both filters
    store.close()


def test_json_export_keeps_all_house_sizes(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    s = Search(name="Cork sharing", category="sharing", params={})
    adapter = FakeAdapter(
        {"Cork sharing": [mkshare("1", 700), mkshare("2", 700)]},
        details={"/share/1": {"_overview": {"sharing with": "9"}}},
    )
    run_cycle(cfg([s], filters={"max_price": 800, "max_sharing_with": 3},
                  publish=pub),
              store, adapter, RecordingNotifier(), logging.getLogger("t"))
    data = json.loads((repo / "listings.json").read_text(encoding="utf-8"))
    # max_sharing_with is NOT applied to the JSON: the big house stays in
    assert {rec["id"] for rec in data["listings"]} == {"1", "2"}
    store.close()


def test_distance_dropped_event_is_suppressed_not_replayed(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="Dublin sharing", category="sharing", params={})
    far = mkshare("d1", 700, lat=_DUBLIN[0] + 0.072, lng=_DUBLIN[1])  # ~8 km N
    notifier = RecordingNotifier()
    log = logging.getLogger("t")

    # cycle 1: 6 km limit -> the NEW event is dropped from the email
    r1 = run_cycle(cfg([s], email_distance_km={"dublin": 6}), store,
                   FakeAdapter({"Dublin sharing": [far]}), notifier, log)
    assert r1.events_sent == 0
    assert store.pending_events() == []  # suppressed, not left pending forever

    # cycle 2: limit widened to 20 km -> the old NEW is NOT replayed
    r2 = run_cycle(cfg([s], email_distance_km={"dublin": 20}), store,
                   FakeAdapter({"Dublin sharing": [far]}), notifier, log)
    assert r2.events_sent == 0
    assert notifier.digests == []
    store.close()


def test_event_for_listing_not_seen_this_cycle_stays_pending(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="Cork sharing", category="sharing", params={})
    log = logging.getLogger("t")

    # cycle 1: listing present but the email blows up -> NEW event stays pending
    with pytest.raises(RuntimeError):
        run_cycle(cfg([s]), store,
                  FakeAdapter({"Cork sharing": [mkshare("1", 700)]}),
                  BrokenNotifier(), log)
    assert [e.listing_id for e in store.pending_events()] == ["1"]

    # cycle 2: listing absent from every search -> NOT suppressed, still pending
    r = run_cycle(cfg([s], min_types=("GONE",)), store,
                  FakeAdapter({"Cork sharing": []}), RecordingNotifier(), log)
    assert r.events_sent == 1  # the GONE event
    assert [e.listing_id for e in store.pending_events()] == ["1"]  # NEW still pending
    store.close()


def test_loop_alerts_on_sustained_publish_failure(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))
    monkeypatch.setattr(
        "daftwatch.runner.export.write_json",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    pub, _repo = _pub(tmp_path)
    s = Search(name="Cork sharing", category="sharing", params={})
    adapter = FakeAdapter({"Cork sharing": [mkshare("1", 700)]})
    notifier = CountingNotifier()
    loop(cfg([s], publish=pub), store, adapter, notifier, logging.getLogger("t"),
         sleeper=lambda x: None, clock=lambda: 0.0, max_cycles=5)
    pub_alerts = [a for a in notifier.alerts if a[0] == "rentals publish failing"]
    assert len(pub_alerts) == 1  # fires at the 3rd consecutive failure, then throttled
    store.close()


def test_loop_publish_failure_counter_resets_on_success(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))
    calls = {"n": 0}

    def flaky_write_json(*a, **k):
        calls["n"] += 1
        if calls["n"] in (1, 2, 4):  # fail, fail, ok, fail, ok...
            raise OSError("disk full")
        return False  # "wrote nothing / unchanged" -> publish_ok True

    monkeypatch.setattr("daftwatch.runner.export.write_json", flaky_write_json)
    pub, _repo = _pub(tmp_path)
    s = Search(name="Cork sharing", category="sharing", params={})
    adapter = FakeAdapter({"Cork sharing": [mkshare("1", 700)]})
    notifier = CountingNotifier()
    loop(cfg([s], publish=pub), store, adapter, notifier, logging.getLogger("t"),
         sleeper=lambda x: None, clock=lambda: 0.0, max_cycles=5)
    assert [a for a in notifier.alerts if a[0] == "rentals publish failing"] == []
    store.close()


def test_loop_closes_adapter_each_iteration(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    s = Search(name="s1", category="rent", params={})
    adapter = FakeAdapter({"s1": [mk("1", 2000)]})
    loop(cfg([s]), store, adapter, CountingNotifier(), logging.getLogger("t"),
         sleeper=lambda x: None, clock=lambda: 0.0, max_cycles=2)
    assert adapter.closed == 2
    store.close()


# --- export_and_publish: the fast path, no adapter/scrape involved ---------

def test_export_and_publish_republishes_from_an_already_synced_store(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    pub, repo = _pub(tmp_path)
    # simulate a DB that a prior scrape already populated — no adapter, no
    # run_cycle, this is exactly what a "republish" CLI invocation sees
    store.begin_cycle()
    store.sync("Cork sharing", [mkshare("d1", 700), mkshare("d2", 900)])
    store.finish_cycle(gone_after_cycles=1)

    ok = export_and_publish(cfg([], publish=pub), store, logging.getLogger("t"))

    assert ok is True
    data = json.loads((repo / "listings.json").read_text(encoding="utf-8"))
    assert {l["id"] for l in data["listings"]} == {"d1", "d2"}
    store.close()


def test_export_and_publish_returns_none_when_publish_not_configured(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    ok = export_and_publish(cfg([], publish=None), store, logging.getLogger("t"))
    assert ok is None
    store.close()


def test_export_and_publish_writes_compare_json_too(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    pub = PublishConfig(
        json_path=str(repo / "listings.json"), repo_dir=str(repo),
        file_rel="listings.json", git_push=False,
        compare_path=str(repo / "compare.json"), compare_rel="compare.json",
    )
    store.sync("Cork sharing", [mkshare(f"d{i}", 800) for i in range(20)])
    store.finish_cycle(gone_after_cycles=1)

    ok = export_and_publish(cfg([], publish=pub), store, logging.getLogger("t"))

    assert ok is True
    assert (repo / "compare.json").exists()
    store.close()
