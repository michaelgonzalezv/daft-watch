from __future__ import annotations

import abc
import logging
import random
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin

from daftwatch.config import Search
from daftwatch.models import Listing, parse_beds, parse_price

_BACKOFF = [30, 60, 120, 240, 300]
_SERVER_PAGE = 50
_DAFT_BASE = "https://www.daft.ie"

_log = logging.getLogger("daftwatch")


class AdapterError(Exception):
    pass


class RateLimited(Exception):
    """Raised by the client when daft.ie answers 429/403 (HTTP layer)."""


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
    """Map one daftlistings 2.0.5 inner ``listing`` dict to our Listing.

    ``d`` is the object daftlistings exposes as ``Listing.as_dict()`` /
    ``self._result`` == ``raw_gateway_result["listing"]``. Every key is treated
    as optional so a schema drift degrades fields rather than crashing.
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


def _default_client() -> Any:
    """Adapter-shaped wrapper around daftlistings 2.0.5.

    This is the ONLY place daftlistings API details live. ``page(n)`` performs
    exactly ONE HTTP POST to the daft gateway with the paging offset set to
    ``n-1``, so the adapter's between-page sleep sits between REAL requests and
    its ``max_pages`` cap genuinely bounds request volume. If daftlistings
    changes, rewrite this function and nothing else.
    """
    import requests
    from daftlistings import Daft, SearchType

    class _Client:
        def __init__(self) -> None:
            self._daft = Daft()
            self._payload: dict | None = None

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
            # Resolve + log what daft actually matched. Daft._get_best_match has
            # no minimum-score floor, so a typo'd slug silently resolves to the
            # nearest place; the log line is the only visibility into that.
            self._payload = self._daft._make_payload()
            _log.info(
                "search resolved: section=%s geo=%s",
                self._payload.get("section"),
                self._payload.get("geoFilter"),
            )

        def page(self, n: int) -> list[dict]:
            payload = dict(self._payload or self._daft._make_payload())
            payload["paging"] = {
                "from": str((n - 1) * _SERVER_PAGE),
                "pagesize": str(_SERVER_PAGE),
            }
            r = requests.post(
                Daft._ENDPOINT,
                headers={
                    **Daft._HEADER,
                    "User-Agent": (
                        "daft-watch/0.1 (personal listings watcher; "
                        "+https://github.com)"
                    ),
                },
                json=payload,
                timeout=30,
            )
            if r.status_code in (429, 403):
                raise RateLimited(f"daft.ie returned {r.status_code}")
            r.raise_for_status()
            body = r.json()
            # body["listings"] must stay loud: a missing key (schema drift or a
            # 200-with-error-body) becomes KeyError -> AdapterError ->
            # adapter_broken, which skips the GONE sweep and fires the
            # broken-scraper alert. A silent [] would route into a mass
            # false-GONE digest after gone_after_cycles.
            return [item["listing"] for item in body["listings"]]

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
            except RateLimited:
                self._sleep(delay)
                continue
            except Exception as exc:  # noqa: BLE001
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
