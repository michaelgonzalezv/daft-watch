from __future__ import annotations

import abc
import json
import logging
import random
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urljoin

from daftwatch.config import Search
from daftwatch.models import Listing, parse_beds, parse_price

_BACKOFF = [30, 60, 120, 240, 300]
_DAFT_BASE = "https://www.daft.ie"

_log = logging.getLogger("daftwatch")

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


class AdapterError(Exception):
    pass


class RateLimited(Exception):
    """Raised when daft.ie answers 429/403 or serves a Cloudflare challenge."""


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coord(d: dict, idx: int):
    try:
        return _f(d["point"]["coordinates"][idx])
    except (KeyError, IndexError, TypeError):
        return None


def to_listing(d: dict, category: str) -> Listing:
    """Map one daft.ie ``__NEXT_DATA__`` inner ``listing`` dict to our Listing.

    ``d`` is one element of ``props.pageProps.listings[i]["listing"]`` from the
    search page's ``__NEXT_DATA__`` blob. Every key is treated as optional so a
    schema drift degrades fields rather than crashing.
    """
    path = d.get("seoFriendlyPath")
    return Listing(
        id=str(d.get("id", "")),
        category=category,
        title=d.get("title") or "",
        url=urljoin(_DAFT_BASE, path) if path else "",
        price_eur=parse_price(d.get("price") or ""),
        beds=parse_beds(d.get("numBedrooms")),
        baths=parse_beds(d.get("numBathrooms")),
        property_type=d.get("propertyType"),
        area=None,
        county=None,
        lat=_coord(d, 1),
        lng=_coord(d, 0),
        raw=d,
    )


class SearchAdapter(abc.ABC):
    @abc.abstractmethod
    def fetch(self, search: Search) -> list[Listing]:
        ...


def _extract_next_data(html: str) -> dict:
    """Pull the parsed ``__NEXT_DATA__`` JSON out of a daft.ie search page.

    A valid page yields the parsed dict. A Cloudflare "Security Check" /
    challenge page has no ``__NEXT_DATA__`` script — that is transient, so raise
    ``RateLimited`` to let the adapter's backoff ladder kick in (NOT
    ``AdapterError``, which would trip the broken-scraper alert).
    """
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise RateLimited("no __NEXT_DATA__ in response (Cloudflare challenge?)")
    return json.loads(m.group(1))


def _build_url(category: str, params: dict, n: int) -> str:
    """Build the daft.ie search URL for page ``n`` (1-based)."""
    sub = "sharing" if category == "sharing" else "property-for-rent"
    locs = [s for s in (params.get("location") or []) if s]
    query: list[tuple[str, Any]] = []
    if len(locs) == 1:
        loc_path = locs[0]
    else:
        loc_path = "ireland"
        query.extend(("location", loc) for loc in locs)
    if "max_price" in params:
        query.append(("rentalPrice_to", params["max_price"]))
    if "min_price" in params:
        query.append(("rentalPrice_from", params["min_price"]))
    if "min_beds" in params:
        query.append(("numBeds_from", params["min_beds"]))
    if "max_beds" in params:
        query.append(("numBeds_to", params["max_beds"]))
    if n > 1:
        query.append(("page", n))
    url = f"{_DAFT_BASE}/{sub}/{loc_path}"
    if query:
        url += "?" + urlencode(query, doseq=True)
    return url


def _default_client() -> Any:
    """Adapter-shaped scraper for daft.ie search pages via ``curl_cffi``.

    This is the ONLY place the daft.ie fetch details live. ``daftlistings`` and
    the gateway API are Cloudflare-blocked (403); instead we fetch the normal
    search HTML with a Chrome TLS fingerprint and read the listings out of the
    page's ``__NEXT_DATA__`` JSON blob. ``page(n)`` performs exactly ONE HTTP GET
    so the adapter's between-page sleep sits between REAL requests and its
    ``max_pages`` cap genuinely bounds request volume. If daft.ie changes its
    Cloudflare config, bump ``impersonate=`` or the ``__NEXT_DATA__`` selector.
    """
    from curl_cffi import requests as _cffi

    class _Client:
        def __init__(self) -> None:
            self._category = "rent"
            self._params: dict = {}
            self._total_pages: int | None = None

        def set_category(self, c: str) -> None:
            self._category = c

        def set_params(self, p: dict) -> None:
            self._params = dict(p)
            _log.info(
                "search url: %s", _build_url(self._category, self._params, 1)
            )

        def page(self, n: int) -> list[dict]:
            if self._total_pages is not None and n > self._total_pages:
                return []
            url = _build_url(self._category, self._params, n)
            r = _cffi.get(url, impersonate="chrome131", timeout=30)
            if r.status_code in (429, 403):
                raise RateLimited(f"daft.ie returned {r.status_code}")
            data = _extract_next_data(r.text)
            # pp / pp["listings"] KeyErrors must stay LOUD: a missing key
            # (schema drift or a 200-with-error-body) becomes KeyError ->
            # AdapterError -> adapter_broken, which skips the GONE sweep and
            # fires the broken-scraper alert. A silent [] would route into a
            # mass false-GONE digest after gone_after_cycles.
            pp = data["props"]["pageProps"]
            try:
                self._total_pages = int(pp["paging"]["totalPages"])
            except (KeyError, TypeError, ValueError):
                pass
            return [item["listing"] for item in pp["listings"]]

    return _Client()


class DaftListingsAdapter(SearchAdapter):
    """Paginating adapter over ``_default_client`` (daft.ie ``__NEXT_DATA__``).

    Name kept for continuity (referenced in ``__main__`` and tests); the fetch
    backend is now ``curl_cffi`` HTML scraping, not the ``daftlistings`` library.
    """

    def __init__(
        self,
        rate_limit_seconds: float = 2.0,
        max_pages: int = 20,
        sleeper: Callable[[float], None] = time.sleep,
        client_factory: Callable[[], Any] = _default_client,
    ):
        self._rate = rate_limit_seconds
        self._max_pages = max_pages
        self._sleep = sleeper
        self._make_client = client_factory

    def _page_with_backoff(self, client: Any, n: int) -> list[dict]:
        for delay in _BACKOFF:
            try:
                return client.page(n)
            except RateLimited:
                self._sleep(delay)
                continue
            except Exception as exc:  # noqa: BLE001
                raise AdapterError(f"daft.ie fetch failed: {exc!r}") from exc
        raise AdapterError("rate-limited by daft.ie; backoff exhausted")

    def fetch(self, search: Search) -> list[Listing]:
        client = self._make_client()
        client.set_category(search.category)
        client.set_params(dict(search.params))

        listings: list[Listing] = []
        prev_count: int | None = None
        for n in range(1, self._max_pages + 1):
            if n > 1:
                self._sleep(self._rate + random.uniform(-0.5, 0.5))
            page = self._page_with_backoff(client, n)
            if not page:
                break
            listings.extend(to_listing(d, search.category) for d in page)
            if prev_count is not None and len(page) < prev_count:
                break
            prev_count = len(page)
        return listings
