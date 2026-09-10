import json
from pathlib import Path

import pytest
from daftwatch.adapter import (
    AdapterError,
    RateLimited,
    SearchAdapter,
    _build_url,
    _extract_next_data,
    _overview_map,
    parse_detail,
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


def _share_listing(i: int) -> dict:
    data = json.loads((FIX / "daft_share_search.json").read_text(encoding="utf-8"))
    return data["props"]["pageProps"]["listings"][i]["listing"]


def _detail_listing() -> dict:
    data = json.loads((FIX / "daft_share_detail.json").read_text(encoding="utf-8"))
    listing = data["props"]["pageProps"]["listing"]
    listing["_overview"] = _overview_map(listing)
    return listing


def test_to_listing_sharing_monthly_range():
    l = to_listing(_share_listing(0), "sharing")
    assert l.source == "daft"
    assert l.currency == "EUR"
    assert l.room_type == "Double & Twin Room"
    assert l.beds is None
    assert l.price_eur == 725  # low end of "From €725 to €750 per month"
    assert l.price_native == 725
    assert l.price_weekly is None
    assert isinstance(l.first_published, str) and l.first_published[:4].isdigit()
    assert len(l.first_published) == 10  # ISO date


def test_to_listing_sharing_weekly():
    l = to_listing(_share_listing(1), "sharing")
    assert l.price_weekly == 160
    assert l.price_eur == round(160 * 52 / 12)
    assert l.price_native == round(160 * 52 / 12)
    assert l.room_type == "Single Room"
    assert l.beds is None


def test_to_listing_rent_keeps_beds_parsing():
    l = to_listing({"id": "5", "numBedrooms": "2 Bed", "price": "€1500 per month"}, "rent")
    assert l.beds == 2
    assert l.room_type is None
    assert l.price_weekly is None


def test_to_listing_bad_publish_date_is_none():
    l = to_listing({"id": "5", "publishDate": "not-a-number"}, "sharing")
    assert l.first_published is None


def test_norm_phone():
    from daftwatch.adapter import _norm_phone
    assert _norm_phone("+353831133127") == "+353831133127"
    assert _norm_phone("083 872 0666") == "+353838720666"
    assert _norm_phone("00353 87 555 0132") == "+353875550132"
    assert _norm_phone("353871234567") == "+353871234567"
    assert _norm_phone("01 456 7890") is None      # landline, not a mobile
    assert _norm_phone(None) is None
    assert _norm_phone("call me") is None
    assert _norm_phone("12345") is None


def test_parse_detail_from_fixture():
    fields = parse_detail(_detail_listing())
    assert set(fields) == {
        "sharing_with", "rooms_available", "preferences", "owner_occupied",
        "available_from", "bathroom_type", "description", "last_updated",
        "agent_phone", "agent_name",
    }
    assert fields["sharing_with"] == 4
    assert fields["rooms_available"] == 1
    assert fields["preferences"] == "Female"
    assert fields["owner_occupied"] is False
    assert fields["available_from"] == "Immediately"
    assert fields["bathroom_type"] == "Shared Bathroom"
    assert fields["description"] and len(fields["description"]) <= 1000
    assert isinstance(fields["last_updated"], str) and len(fields["last_updated"]) == 10


def test_parse_detail_empty_overview_all_none():
    fields = parse_detail({"_overview": {}})
    assert set(fields) == {
        "sharing_with", "rooms_available", "preferences", "owner_occupied",
        "available_from", "bathroom_type", "description", "last_updated",
        "agent_phone", "agent_name",
    }
    assert all(v is None for v in fields.values())


def test_parse_detail_last_updated_falls_back_to_first_publish():
    fields = parse_detail({"_overview": {}, "firstPublishDate": 1788373250189})
    assert isinstance(fields["last_updated"], str) and len(fields["last_updated"]) == 10


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
    def __init__(self, pages, detail_result=None):
        self._pages = pages
        self._detail_result = detail_result if detail_result is not None else {"_overview": {}}
        self.category = None
        self.params = None
        self.calls = []
        self.detail_calls = []

    def set_category(self, c): self.category = c
    def set_params(self, p): self.params = p

    def page(self, n):
        self.calls.append(n)
        return self._pages[n - 1] if n - 1 < len(self._pages) else []

    def detail(self, path):
        self.detail_calls.append(path)
        return self._detail_result

    def close(self): pass


class BoomClient:
    def __init__(self, exc): self.exc = exc
    def set_category(self, c): pass
    def set_params(self, p): pass
    def page(self, n): raise self.exc
    def detail(self, path): raise self.exc
    def close(self): pass


class CountingFactory:
    """client_factory that records how many times it was called."""

    def __init__(self, client):
        self.client = client
        self.count = 0

    def __call__(self):
        self.count += 1
        return self.client


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


def _next_data_html(listings, total_pages):
    blob = json.dumps(
        {"props": {"pageProps": {"listings": listings, "paging": {"totalPages": total_pages}}}}
    )
    return f'<script id="__NEXT_DATA__" type="application/json">{blob}</script>'


class _FakePwPage:
    def __init__(self, html): self._html = html
    def goto(self, *a, **k): pass
    def wait_for_selector(self, *a, **k): pass
    def content(self): return self._html
    def close(self): pass


class _FakePwCtx:
    def __init__(self, htmls):
        self._htmls = list(htmls)
        self.navigations = 0
    def new_page(self):
        self.navigations += 1
        return _FakePwPage(self._htmls.pop(0))
    def storage_state(self, *a, **k): pass


def test_client_set_params_resets_pagination_state():
    from daftwatch.adapter import _default_client

    client = _default_client()
    client._total_pages = 0  # left over from a prior empty search
    client.set_params({"location": ["cork-city-cork"]})
    assert client._total_pages is None


def test_reused_client_empty_search_does_not_short_circuit_next_search(monkeypatch):
    """Search A returns totalPages == 0; search B's page(1) must still navigate."""
    from daftwatch.adapter import _default_client

    client = _default_client()
    ctx = _FakePwCtx([
        _next_data_html([], 0),  # search A, page 1: empty result set
        _next_data_html([{"listing": {"id": "b1", "price": "€1 per month"}}], 1),  # search B, page 1
        _next_data_html([], 1),  # search B, page 2: empty -> loop stops
    ])
    monkeypatch.setattr(client, "_ensure", lambda: None)
    client._ctx = ctx

    client.set_category("sharing")
    client.set_params({"location": ["niche-a"]})
    assert client.page(1) == []
    assert client._total_pages == 0

    client.set_params({"location": ["niche-b"]})
    assert client._total_pages is None  # reset for the new search
    out = client.page(1)
    assert [d["id"] for d in out] == ["b1"]
    assert ctx.navigations == 2  # search B genuinely navigated


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


def test_fetch_does_not_close_client_and_close_does():
    fake = CloseTrackingFakeClient([[{"id": "1", "price": "€1 per month"}]])
    _, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(sleeper=sleeper, client_factory=lambda: fake)
    a.fetch(Search(name="s", category="rent", params={}))
    assert fake.closed == 0  # fetch reuses the client; runner owns close()
    a.close()
    assert fake.closed == 1
    a.close()  # idempotent
    assert fake.closed == 1


def test_close_after_fetch_error_still_closes():
    boom = CloseTrackingBoomClient(RuntimeError("boom"))
    _, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(sleeper=sleeper, client_factory=lambda: boom)
    with pytest.raises(AdapterError):
        a.fetch(Search(name="s", category="rent", params={}))
    a.close()
    assert boom.closed == 1


def test_fetch_then_detail_reuse_the_same_client():
    fake = FakeClient(
        [[{"id": "1", "price": "€1 per month"}]],
        detail_result={"_overview": {"sharing with": "3"}, "bathroomType": "Ensuite"},
    )
    factory = CountingFactory(fake)
    _, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(sleeper=sleeper, client_factory=factory)
    a.fetch(Search(name="s", category="sharing", params={}))
    detail = a.detail("/share/x/1")
    assert factory.count == 1  # one client for fetch + detail
    assert fake.detail_calls == ["/share/x/1"]
    assert parse_detail(detail)["sharing_with"] == 3


def test_detail_backoff_then_error_on_rate_limited():
    slept, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(
        sleeper=sleeper,
        client_factory=lambda: BoomClient(RateLimited("daft.ie returned 429")),
    )
    with pytest.raises(AdapterError):
        a.detail("/share/x/1")
    assert slept[:5] == [30, 60, 120, 240, 300]


def test_detail_wraps_other_errors_without_retry():
    slept, sleeper = _rec_sleeper()
    a = DaftListingsAdapter(
        sleeper=sleeper,
        client_factory=lambda: BoomClient(RuntimeError("boom")),
    )
    with pytest.raises(AdapterError):
        a.detail("/share/x/1")
    assert slept == []


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


@pytest.mark.live
def test_default_client_detail_live():
    """Real headless Chromium fetch of one sharing detail page; excluded from CI."""
    from daftwatch.adapter import _default_client

    client = _default_client()
    try:
        client.set_category("sharing")
        client.set_params({"location": ["cork-city-cork"]})
        page = client.page(1)
        assert page, "no sharing listings on cork-city right now"
        path = page[0]["seoFriendlyPath"]
        detail = client.detail(path)
        assert isinstance(detail, dict)
        assert "_overview" in detail
        # must not raise regardless of which overview fields the page carries
        fields = parse_detail(detail)
        assert set(fields) == {
            "sharing_with", "rooms_available", "preferences", "owner_occupied",
            "available_from", "bathroom_type", "description", "last_updated",
            "agent_phone", "agent_name",
        }
    finally:
        client.close()


@pytest.mark.live
def test_adapter_detail_live():
    from daftwatch.config import Search as _S

    # no-op sleeper: a rate-limited environment should fail fast, not sleep the
    # full 12-minute backoff ladder.
    a = DaftListingsAdapter(sleeper=lambda _s: None)
    try:
        got = a.fetch(_S(name="cork", category="sharing", params={"location": ["cork-city-cork"]}))
        assert got
        path = got[0].url.replace("https://www.daft.ie", "")
        detail = a.detail(path)
        assert isinstance(detail, dict)
        parse_detail(detail)  # does not raise
    finally:
        a.close()
