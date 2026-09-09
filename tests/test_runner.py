import logging

import pytest

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


class BrokenNotifier:
    def send_digest(self, items): raise RuntimeError("smtp down")
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
