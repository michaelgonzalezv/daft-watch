import json
from pathlib import Path

import pytest
from daftwatch.adapter import SearchAdapter, AdapterError, RateLimited, to_listing

FIX = Path(__file__).parent / "fixtures"

# NOTE: daft_rent_page1.json is HAND-BUILT to the daftlistings 2.0.5 inner
# `listing` schema (`Listing.as_dict()` == raw_gateway_result["listing"]).
# A live capture is 403-blocked from CI; replace with a real
# `Daft().search(max_pages=1)[0].as_dict()` capture once run from a non-blocked
# network.


def test_to_listing_monthly_real_keys():
    d = json.loads((FIX / "daft_rent_page1.json").read_text())[0]
    l = to_listing(d, "rent")
    assert l.id == "5001"
    assert l.category == "rent"
    assert l.price_eur == 2100
    assert l.beds == 2
    assert l.baths == 1
    assert l.property_type == "Apartment"
    # seoFriendlyPath is relative -> URL must be absolute
    assert l.url == "https://www.daft.ie/for-rent/apartment-rialto-dublin-8/5001234"
    assert l.lat == 53.3331
    assert l.lng == -6.2925


def test_to_listing_weekly_converts_and_studio_has_no_beds():
    d = json.loads((FIX / "daft_rent_page1.json").read_text())[1]
    l = to_listing(d, "rent")
    assert l.price_eur == round(425 * 52 / 12)
    assert l.beds is None  # "Studio" -> no number
    assert l.baths is None  # numBathrooms key absent


def test_to_listing_tolerates_missing_keys():
    l = to_listing({"id": "9"}, "sharing")
    assert l.id == "9"
    assert l.price_eur == 0
    assert l.beds is None
    assert l.url == ""
    assert l.lat is None
    assert l.property_type is None


def test_searchadapter_is_abstract():
    with pytest.raises(TypeError):
        SearchAdapter()


from daftwatch.adapter import DaftListingsAdapter
from daftwatch.config import Search


class FakeClient:
    def __init__(self, pages):
        self._pages = pages
        self.category = None
        self.params = None
        self.calls = []

    def set_category(self, c): self.category = c
    def set_params(self, p): self.params = p

    def page(self, n):
        self.calls.append(n)
        return self._pages[n - 1] if n - 1 < len(self._pages) else []


class BoomClient:
    def __init__(self, exc): self.exc = exc
    def set_category(self, c): pass
    def set_params(self, p): pass
    def page(self, n): raise self.exc


def _rec_sleeper():
    slept = []
    return slept, (lambda s: slept.append(s))


def test_fetch_paginates_and_maps():
    p1 = [{"id": "1", "price": "€1000 per month"},
          {"id": "2", "price": "€1100 per month"}]
    p2 = [{"id": "3", "price": "€1200 per month"}]   # shorter -> last page
    fake = FakeClient([p1, p2])
    slept, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(rate_limit_seconds=2, sleeper=sleeper,
                            client_factory=lambda: fake)
    out = a.fetch(Search(name="s", category="rent", params={"max_price": 1500}))
    assert [l.id for l in out] == ["1", "2", "3"]
    assert fake.category == "rent"
    assert fake.params == {"max_price": 1500}
    assert fake.calls == [1, 2]
    assert len(slept) >= 1 and all(1.5 <= s <= 2.5 for s in slept)


def test_fetch_stops_at_max_pages():
    full = [{"id": str(i), "price": "€1 per month"} for i in range(3)]
    fake = FakeClient([full] * 50)
    _, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(sleeper=sleeper, max_pages=4, client_factory=lambda: fake)
    a.fetch(Search(name="s", category="rent", params={}))
    assert fake.calls == [1, 2, 3, 4]


def test_fetch_backoff_then_error_on_rate_limited():
    slept, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(
        sleeper=sleeper,
        client_factory=lambda: BoomClient(RateLimited("daft.ie returned 429")),
    )
    with pytest.raises(AdapterError):
        a.fetch(Search(name="s", category="rent", params={}))
    assert slept[:5] == [30, 60, 120, 240, 300]


def test_fetch_wraps_other_errors_without_retry():
    slept, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(
        sleeper=sleeper,
        client_factory=lambda: BoomClient(RuntimeError("boom")),
    )
    with pytest.raises(AdapterError):
        a.fetch(Search(name="s", category="rent", params={}))
    assert slept == []  # generic errors are not retried
