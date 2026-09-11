import urllib.error

import pytest
from daftwatch.fx import DEFAULT_RATES_USD, fetch_rates_usd


def test_usd_is_always_one_no_network_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should never fetch USD over the network")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert fetch_rates_usd(["USD"]) == {"USD": 1.0}


def test_fetch_uses_response_rate(monkeypatch):
    import json as _json

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return _json.dumps({"rates": {"USD": 1.2345}}).encode()

    seen_urls = []

    def fake_urlopen(req, timeout=None):
        seen_urls.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    rates = fetch_rates_usd(["EUR"])
    assert rates == {"EUR": 1.2345}
    assert "from=EUR" in seen_urls[0] and "to=USD" in seen_urls[0]


def test_fetch_falls_back_on_network_error(monkeypatch):
    def fail(*a, **k):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr("urllib.request.urlopen", fail)
    assert fetch_rates_usd(["EUR", "CAD"]) == {
        "EUR": DEFAULT_RATES_USD["EUR"], "CAD": DEFAULT_RATES_USD["CAD"],
    }


def test_fetch_falls_back_on_malformed_response(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"not json"

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: FakeResp())
    assert fetch_rates_usd(["EUR"]) == {"EUR": DEFAULT_RATES_USD["EUR"]}


def test_unknown_currency_falls_back_to_one(monkeypatch):
    def fail(*a, **k):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr("urllib.request.urlopen", fail)
    assert fetch_rates_usd(["XYZ"]) == {"XYZ": 1.0}
