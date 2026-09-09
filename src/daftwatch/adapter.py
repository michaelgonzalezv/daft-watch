from __future__ import annotations

import abc
import random
import time
from collections.abc import Callable
from typing import Any

from daftwatch.config import Search
from daftwatch.models import Listing, parse_beds, parse_price

_BACKOFF = [30, 60, 120, 240, 300]
_DEFAULT_MAX_PAGES = 20
_SERVER_PAGE = 50


class AdapterError(Exception):
    pass


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_listing(d: dict, category: str) -> Listing:
    return Listing(
        id=str(d.get("id", "")),
        category=category,
        title=d.get("title") or "",
        url=d.get("daft_link") or d.get("url") or "",
        price_eur=parse_price(d.get("price") or ""),
        beds=parse_beds(d.get("bedrooms")),
        baths=parse_beds(d.get("bathrooms")),
        property_type=d.get("category"),
        area=d.get("location") or d.get("area"),
        county=d.get("county"),
        lat=_f(d.get("latitude")),
        lng=_f(d.get("longitude")),
        raw=d,
    )


class SearchAdapter(abc.ABC):
    @abc.abstractmethod
    def fetch(self, search: Search) -> list[Listing]:
        ...


def _default_client() -> Any:
    """Adapter-shaped wrapper around daftlistings 2.0.5.

    This is the ONLY place daftlistings API details live. If daftlistings
    changes, rewrite this function and nothing else. daftlistings does its
    own pagination inside search(); we fetch once and hand the adapter
    50-item slices so the adapter's page loop still works.
    """
    from daftlistings import Daft, SearchType

    class _Client:
        def __init__(self) -> None:
            self._daft = Daft()
            try:
                self._daft._HEADER = {
                    **Daft._HEADER,
                    "User-Agent": "daft-watch/0.1 (personal listings watcher)",
                }
            except Exception:
                pass
            self._cache: list[dict] | None = None

        def set_category(self, c: str) -> None:
            self._daft.set_search_type(
                SearchType.SHARING if c == "sharing"
                else SearchType.RESIDENTIAL_RENT
            )

        def set_params(self, p: dict) -> None:
            for loc in p.get("location", []):
                self._daft.set_location(loc)
            if "min_price" in p:
                self._daft.set_min_price(p["min_price"])
            if "max_price" in p:
                self._daft.set_max_price(p["max_price"])
            if "min_beds" in p:
                self._daft.set_min_beds(p["min_beds"])
            if "max_beds" in p:
                self._daft.set_max_beds(p["max_beds"])

        def page(self, n: int) -> list[dict]:
            if self._cache is None:
                results = self._daft.search(max_pages=_DEFAULT_MAX_PAGES)
                self._cache = [r.as_dict() for r in results]
            start = (n - 1) * _SERVER_PAGE
            return self._cache[start:start + _SERVER_PAGE]

    return _Client()


class DaftListingsAdapter(SearchAdapter):
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
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                if "429" in text or "403" in text:
                    self._sleep(delay)
                    continue
                raise AdapterError(f"daftlistings failed: {exc!r}") from exc
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
