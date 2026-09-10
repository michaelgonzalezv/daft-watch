from __future__ import annotations

import abc
import json
import logging
import os
import random
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urljoin

from daftwatch.config import Search
from daftwatch.models import (
    Listing,
    parse_beds,
    parse_int,
    parse_price_range,
)

_BACKOFF = [30, 60, 120, 240, 300]
_DAFT_BASE = "https://www.daft.ie"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Where the persisted Playwright storage state (the cf_clearance cookie) lives.
# Defaults to the Docker data volume; may not be writable for local runs, so
# every read/write of it is best-effort.
_STORAGE = os.environ.get("DAFT_WATCH_STATE", "/data/pw-state.json")

_log = logging.getLogger("daftwatch")

# daft.ie now renders the tag as
#   <script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">
# so tolerate any extra attributes after the type before the closing ">".
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.S,
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


def _iso_from_ms(ms: Any) -> str | None:
    """ISO date (UTC) from a millisecond epoch, or None for bad/absent values."""
    try:
        return (
            datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
            .date()
            .isoformat()
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def to_listing(d: dict, category: str) -> Listing:
    """Map one daft.ie ``__NEXT_DATA__`` inner ``listing`` dict to our Listing.

    ``d`` is one element of ``props.pageProps.listings[i]["listing"]`` from the
    search page's ``__NEXT_DATA__`` blob. Every key is treated as optional so a
    schema drift degrades fields rather than crashing.
    """
    path = d.get("seoFriendlyPath")
    price_text = d.get("price") or ""
    price = parse_price_range(price_text)
    weekly = "per week" in price_text.lower()

    if category == "sharing":
        beds = None
        room_type = d.get("numBedrooms")
    else:
        beds = parse_beds(d.get("numBedrooms"))
        room_type = None

    return Listing(
        id=str(d.get("id", "")),
        category=category,
        title=d.get("title") or "",
        url=urljoin(_DAFT_BASE, path) if path else "",
        price_eur=price,
        beds=beds,
        baths=parse_beds(d.get("numBathrooms")),
        property_type=d.get("propertyType"),
        area=None,
        county=None,
        lat=_coord(d, 1),
        lng=_coord(d, 0),
        raw=d,
        source="daft",
        currency="EUR",
        country="Ireland",
        price_native=price,
        price_weekly=parse_int(price_text.replace(",", "")) if weekly else None,
        first_published=_iso_from_ms(d.get("publishDate")),
        room_type=room_type,
    )


def _overview_map(listing: dict) -> dict[str, str]:
    """Flatten a detail page's ``propertyOverview`` list to a ``{label: text}``
    map with lower-cased, stripped labels."""
    out: dict[str, str] = {}
    for item in listing.get("propertyOverview") or []:
        label = item.get("label")
        if not label:
            continue  # a malformed overview item must not crash detail()
        out[label.strip().lower()] = (item.get("text") or "").strip()
    return out


def _yn(text: Any) -> bool | None:
    """"Yes"/"No" (case-insensitive) → True/False; anything else → None."""
    if not isinstance(text, str):
        return None
    t = text.strip().lower()
    if t == "yes":
        return True
    if t == "no":
        return False
    return None


def parse_detail(listing_dict: dict) -> dict:
    """Pure map of a detail-page ``listing`` dict (with ``_overview``) to the
    8 enrichment fields ``store.apply_detail`` expects."""
    ov = listing_dict.get("_overview", {})
    description = (listing_dict.get("description") or "").strip()[:1000] or None
    last_updated = _iso_from_ms(listing_dict.get("lastUpdateDate")) or _iso_from_ms(
        listing_dict.get("firstPublishDate")
    )
    return {
        "sharing_with": parse_int(ov.get("sharing with")),
        "rooms_available": parse_int(ov.get("bedrooms available")),
        "preferences": ov.get("preferences") or None,
        "owner_occupied": _yn(ov.get("owner occupied")),
        "available_from": ov.get("available from") or None,
        "bathroom_type": listing_dict.get("bathroomType"),
        "description": description,
        "last_updated": last_updated,
    }


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
    """Adapter-shaped daft.ie scraper backed by headless Chromium (Playwright).

    daft.ie sits behind Cloudflare's managed challenge; only a real browser
    clears it reliably. One headless Chromium is launched lazily on the first
    navigation and REUSED for the whole adapter lifecycle (every search's
    ``fetch`` plus the detail loop); ``DaftListingsAdapter.close()`` — called by
    the runner in a ``finally`` — tears it down. A persisted storage state (the
    cf_clearance cookie) is reused across restarts so most navigations skip the
    interstitial. ``page(n)`` / ``detail(path)`` each perform exactly ONE
    navigation so the adapter's between-page sleep sits between REAL requests and
    its ``max_pages`` cap genuinely bounds request volume. ``set_params`` resets
    the per-search pagination state so a prior search's ``totalPages`` never
    short-circuits the next one. If daft.ie tightens Cloudflare, options are a
    newer Chromium, ``channel="chrome"``, or a longer interstitial wait.
    """
    from playwright.sync_api import sync_playwright

    class _Client:
        def __init__(self) -> None:
            self._category = "rent"
            self._params: dict = {}
            self._total_pages: int | None = None
            self._pw = None
            self._browser = None
            self._ctx = None

        def _ensure(self):
            if self._ctx is not None:
                return
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            kw: dict = {
                "user_agent": _UA,
                "viewport": {"width": 1280, "height": 900},
                "locale": "en-IE",
            }
            if os.path.exists(_STORAGE):
                kw["storage_state"] = _STORAGE
            self._ctx = self._browser.new_context(**kw)

        def set_category(self, c: str) -> None:
            self._category = c

        def set_params(self, p: dict) -> None:
            self._params = dict(p)
            # Reset pagination state: a previous search's totalPages (0 for an
            # empty result set) must not short-circuit this search's page(1).
            self._total_pages = None
            _log.info(
                "search url: %s", _build_url(self._category, self._params, 1)
            )

        def page(self, n: int) -> list[dict]:
            if self._total_pages is not None and n > self._total_pages:
                return []
            self._ensure()
            url = _build_url(self._category, self._params, n)
            pg = self._ctx.new_page()
            try:
                pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                # Cloudflare interstitial ("Just a moment...") clears itself in
                # a real browser; wait for the real page's __NEXT_DATA__.
                try:
                    # state="attached": a <script> is never "visible", and the
                    # blob is what we parse, not something rendered.
                    pg.wait_for_selector(
                        "script#__NEXT_DATA__",
                        state="attached",
                        timeout=25000,
                    )
                except Exception:  # noqa: BLE001
                    pass
                html = pg.content()
            finally:
                pg.close()
            # persist cookies (cf_clearance) for next time — best effort
            try:
                self._ctx.storage_state(path=_STORAGE)
            except Exception:  # noqa: BLE001
                pass
            # raises RateLimited if still challenged (no __NEXT_DATA__)
            data = _extract_next_data(html)
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

        def detail(self, path: str) -> dict | None:
            self._ensure()
            url = urljoin(_DAFT_BASE, path)
            pg = self._ctx.new_page()
            try:
                pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    pg.wait_for_selector(
                        "script#__NEXT_DATA__",
                        state="attached",
                        timeout=25000,
                    )
                except Exception:  # noqa: BLE001
                    pass
                html = pg.content()
            finally:
                pg.close()
            try:
                self._ctx.storage_state(path=_STORAGE)
            except Exception:  # noqa: BLE001
                pass
            # raises RateLimited if still challenged (no __NEXT_DATA__)
            data = _extract_next_data(html)
            # props / pageProps missing = real schema drift -> LOUD KeyError
            # -> AdapterError upstream.
            pp = data["props"]["pageProps"]
            listing = pp.get("listing")
            if listing is None:
                # Page rendered but has no listing: the ad was delisted between
                # the search page that surfaced it and this fetch. Not an error
                # -> signal the caller to skip it.
                return None
            listing["_overview"] = _overview_map(listing)
            return listing

        def close(self) -> None:
            for obj, meth in (
                (self._ctx, "close"),
                (self._browser, "close"),
            ):
                try:
                    if obj is not None:
                        getattr(obj, meth)()
                except Exception:  # noqa: BLE001
                    pass
            try:
                if self._pw is not None:
                    self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._ctx = self._browser = self._pw = None

    return _Client()


class DaftListingsAdapter(SearchAdapter):
    """Paginating adapter over ``_default_client`` (daft.ie ``__NEXT_DATA__``).

    Name kept for continuity (referenced in ``__main__`` and tests); the fetch
    backend is now headless Chromium via Playwright, not the ``daftlistings``
    library.
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
        # One client is lazily created and reused for the whole cycle (fetch +
        # the detail loop). The runner calls close() in a finally.
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def _with_backoff(self, op: str, fn: Callable[[], Any]) -> Any:
        """Run ``fn``; on RateLimited sleep the backoff ladder and retry, then
        give up with AdapterError. Any other error → AdapterError (no retry).
        ``op`` names the operation for the error message (e.g. ``"page 2"``,
        ``"detail /share/…"``)."""
        for delay in _BACKOFF:
            try:
                return fn()
            except RateLimited:
                self._sleep(delay)
                continue
            except Exception as exc:  # noqa: BLE001
                raise AdapterError(
                    f"daft.ie {op} failed: {exc!r}"
                ) from exc
        raise AdapterError(
            f"rate-limited by daft.ie ({op}); backoff exhausted"
        )

    def _page_with_backoff(self, client: Any, n: int) -> list[dict]:
        return self._with_backoff(f"page {n}", lambda: client.page(n))

    def detail(self, path: str) -> dict | None:
        client = self._ensure_client()
        return self._with_backoff(
            f"detail {path}", lambda: client.detail(path)
        )

    def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
            self._client = None

    def fetch(self, search: Search) -> list[Listing]:
        client = self._ensure_client()
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
