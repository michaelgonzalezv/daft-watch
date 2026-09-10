import daftwatch.watchlist as w
from daftwatch.watchlist import fetch_watchlist


def test_fetch_watchlist_no_config():
    assert fetch_watchlist(None, "k") == set()
    assert fetch_watchlist("http://x", None) == set()
    assert fetch_watchlist("", "") == set()


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_fetch_watchlist_parses_ids(monkeypatch):
    monkeypatch.setattr(
        w.urllib.request, "urlopen",
        lambda req, timeout: _FakeResp(b'{"ids": ["1", "2", 3]}'),
    )
    assert fetch_watchlist("http://x/api/favs", "key") == {"1", "2", "3"}


def test_fetch_watchlist_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(w.urllib.request, "urlopen", boom)
    assert fetch_watchlist("http://x", "key") == set()
