import json
from pathlib import Path

import pytest
from daftwatch.adapter import (
    AdapterError,
    RateLimited,
    SearchAdapter,
    _build_url,
    _extract_next_data,
    to_listing,
)

FIX = Path(__file__).parent / "fixtures"

# daft_next_data_page1.json is a REAL 2-listing capture of a daft.ie search
# page's __NEXT_DATA__ blob: full {"props":{"pageProps":{...}}} shape, one
# monthly studio (synthetic numBathrooms added) and one weekly PRS 1-bed.


def _listing(i: int) -> dict:
    data = json.loads((FIX / "daft_next_data_page1.json").read_text(encoding="utf-8"))
    return data["props"]["pageProps"]["listings"][i]["listing"]


def test_to_listing_monthly_studio_real_keys():
    src = _listing(0)
    l = to_listing(src, "rent")
    assert isinstance(l.id, str) and l.id == "6504377"
    assert l.category == "rent"
    assert l.price_eur == 1495  # "€1,495 per month"
    assert l.beds is None  # numBedrooms absent for studios
    assert l.baths == 1  # "1 bath"
    assert l.property_type == "Studio"  # propertyType, not category ("Rent")
    assert l.url.startswith("https://www.daft.ie/")
    assert l.url.endswith(src["seoFriendlyPath"])
    assert l.lat == pytest.approx(53.337, abs=1e-2)
    assert l.lng == pytest.approx(-6.318, abs=1e-2)


def test_to_listing_weekly_prs_converts_price():
    src = _listing(1)
    l = to_listing(src, "rent")
    assert l.price_eur == round(305 * 52 / 12)  # "From €305 per week"
    assert l.beds == 1  # "1 bed"
    assert l.baths is None  # numBathrooms absent
    assert l.property_type == "Private Rental Sector"
    assert l.url.endswith(src["seoFriendlyPath"])


def test_to_listing_tolerates_missing_keys():
    l = to_listing({"id": "9"}, "sharing")
    assert l.id == "9"
    assert l.price_eur == 0
    assert l.beds is None
    assert l.url == ""
    assert l.lat is None
    assert l.property_type is None


def test_extract_next_data_parses_valid_html():
    blob = '{"props": {"pageProps": {"listings": [], "paging": {"totalPages": 1}}}}'
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + blob
        + "</script></body></html>"
    )
    data = _extract_next_data(html)
    assert data["props"]["pageProps"]["listings"] == []


def test_extract_next_data_raises_ratelimited_on_challenge_page():
    with pytest.raises(RateLimited):
        _extract_next_data("<html><body>Security Check | Daft</body></html>")


def test_build_url_single_location_in_path():
    url = _build_url("rent", {"location": ["dublin-8-dublin"], "max_price": 2200}, 1)
    assert url == (
        "https://www.daft.ie/property-for-rent/dublin-8-dublin?rentalPrice_to=2200"
    )


def test_build_url_multi_location_and_paging():
    url = _build_url(
        "sharing",
        {"location": ["dublin-8-dublin", "dublin-6-dublin"], "min_beds": 1},
        3,
    )
    assert url == (
        "https://www.daft.ie/sharing/ireland"
        "?location=dublin-8-dublin&location=dublin-6-dublin&numBeds_from=1&page=3"
    )


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


class CloseTrackingFakeClient(FakeClient):
    """FakeClient that also records close() calls (the Playwright client has one)."""

    def __init__(self, pages):
        super().__init__(pages)
        self.closed = 0

    def close(self):
        self.closed += 1


class CloseTrackingBoomClient(BoomClient):
    def __init__(self, exc):
        super().__init__(exc)
        self.closed = 0

    def close(self):
        self.closed += 1


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


def test_fetch_closes_client_when_present():
    fake = CloseTrackingFakeClient([[{"id": "1", "price": "€1 per month"}]])
    _, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(sleeper=sleeper, client_factory=lambda: fake)
    a.fetch(Search(name="s", category="rent", params={}))
    assert fake.closed == 1


def test_fetch_closes_client_even_when_fetch_raises():
    boom = CloseTrackingBoomClient(RuntimeError("boom"))
    _, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(sleeper=sleeper, client_factory=lambda: boom)
    with pytest.raises(AdapterError):
        a.fetch(Search(name="s", category="rent", params={}))
    assert boom.closed == 1


@pytest.mark.live
def test_default_client_live_hits_daft():
    """Launches real headless Chromium against daft.ie; excluded from the default run."""
    from daftwatch.adapter import _default_client

    client = _default_client()
    try:
        client.set_category("rent")
        client.set_params({"location": ["dublin-8-dublin"], "max_price": 2200})
        out = client.page(1)
        assert isinstance(out, list) and out
        first = out[0]
        assert isinstance(first, dict)
        assert "id" in first and "title" in first and "price" in first
    finally:
        client.close()
