from __future__ import annotations

import logging
import re
import time
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError

from daftwatch.adapter import AdapterError, RateLimited, SearchAdapter, _extract_next_data
from daftwatch.config import Search
from daftwatch.models import Listing

_BACKOFF = [30, 60, 120, 240, 300]
_KIJIJI_BASE = "https://www.kijiji.ca"
# "Room Rentals & Roommates" — confirmed by inspecting the site's own category
# filter tree (kijiji.ca/b-room-rental-roommate/.../c36l<location-id>). The
# URL's city slug is cosmetic; only the numeric location id after "l" and the
# category id after "c" route the search, so every search always uses the
# same generic "canada" slug.
_CATEGORY_ID = 36

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_log = logging.getLogger("daftwatch")

# Kijiji's "Room Rentals & Roommates" category has no structured house/
# apartment field (confirmed against the category's own filter definition —
# only furnished/petsallowed exist as real attributes), so property_type is
# guessed from title+description keywords. Measured against 46 real Toronto
# listings: ~59% get a classification, 0% had both a house AND an apartment
# keyword. A standalone "basement" (no house/apartment word either) gets its
# own bucket rather than being folded into either guess — in Toronto listings
# that usually means a self-contained basement apartment, but that's an
# inference, not confirmed, so it stays visibly distinct instead of silently
# becoming "Apartment".
_HOUSE_RE = re.compile(
    r"\b(house|townhouse|town house|bungalow|semi-detached|duplex|triplex|detached)\b",
    re.I,
)
_APT_RE = re.compile(r"\b(apartment|apt\.?|condo|condominium|flat)\b", re.I)
_BASEMENT_RE = re.compile(r"\bbasement\b", re.I)


def _classify_property_type(title: str, description: str) -> str:
    text = f"{title or ''} {description or ''}"
    has_house = bool(_HOUSE_RE.search(text))
    has_apt = bool(_APT_RE.search(text))
    if has_house and has_apt:
        return "Room"  # genuine conflict in the text — don't guess which wins
    if has_house:
        return "House"
    if has_apt:
        return "Apartment"
    if _BASEMENT_RE.search(text):
        return "Basement"
    return "Room"  # no signal either way — today's default, unchanged


# Gender preference — same category, same "no structured field" problem, but
# noisier text than house/apartment: a first pass matching any bare "male"/
# "female" produced real false positives (a form asking the APPLICANT'S own
# gender: "Name + male/female + age"; a description of the CURRENT roommates:
# "kitchen shared with other males", which isn't necessarily a request for
# more of the same). Two tiers instead of one bare-word match:
#   - "only" phrasing (either word order: "female only" / "only for female")
#     is an unambiguous restriction -> "<Gender> only"
#   - a preference verb (looking for / seeking / prefer / want / need /
#     suit(able) / ideal for) within 3 words of "male"/"female" is a genuine
#     ask, not incidental mention -> "<Gender> preferred"
# Measured against the same 46 real Toronto listings: 10/46 (22%) matched,
# manually checked every match — no false positives in that pass (the two
# false-positive patterns above no longer match either tier).
_GENDER_ONLY_RE = {
    "Female": re.compile(
        r"\b(females?[\s-]*only|only\s+(?:is\s+)?(?:for\s+)?females?|"
        r"women[\s-]*only|ladies[\s-]*only|no\s+males?)\b",
        re.I,
    ),
    "Male": re.compile(
        r"\b(males?[\s-]*only|only\s+(?:is\s+)?(?:for\s+)?males?|"
        r"men[\s-]*only|no\s+females?)\b",
        re.I,
    ),
}
_PREF_VERB = r"(?:looking for|seeking|prefer(?:ably|red)?|suit(?:able)?|ideal for|want(?:ed)?|need(?:ed)?)"
_GENDER_PREF_RE = {
    "Female": re.compile(rf"\b{_PREF_VERB}\b(?:\s+\w+){{0,3}}\s+females?\b", re.I),
    "Male": re.compile(rf"\b{_PREF_VERB}\b(?:\s+\w+){{0,3}}\s+males?\b", re.I),
}


def _classify_gender_pref(title: str, description: str) -> str | None:
    text = f"{title or ''} {description or ''}"
    only_hits = [g for g, rx in _GENDER_ONLY_RE.items() if rx.search(text)]
    if len(only_hits) == 1:
        return f"{only_hits[0]} only"
    if only_hits:
        return None  # both "only" patterns hit — contradictory, don't guess
    pref_hits = [g for g, rx in _GENDER_PREF_RE.items() if rx.search(text)]
    if len(pref_hits) == 1:
        return f"{pref_hits[0]} preferred"
    return None  # both, or neither — no clean signal


def _fetch_html(url: str, timeout: float = 30.0) -> str:
    """Plain HTTP GET — kijiji.ca serves its full __NEXT_DATA__ page to a bare
    request, no headless browser needed (unlike daft.ie's Cloudflare gate)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status in (429, 403):
                raise RateLimited(f"kijiji.ca returned {resp.status}")
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code in (429, 403):
            raise RateLimited(f"kijiji.ca returned {exc.code}") from exc
        raise
    except URLError as exc:
        raise RateLimited(f"kijiji.ca unreachable: {exc}") from exc


def _search_url(location_id: str) -> str:
    return f"{_KIJIJI_BASE}/b-room-rental-roommate/canada/c{_CATEGORY_ID}l{location_id}"


def _price_native(entry: dict) -> int:
    """Kijiji prices are integer cents; 0 for anything malformed or missing
    (e.g. "Please Contact")."""
    try:
        amount = entry["price"]["amount"]
    except (KeyError, TypeError):
        return 0
    if not isinstance(amount, (int, float)):
        return 0
    return round(amount / 100)


def _norm_phone_ca(raw: Any) -> str | None:
    """Canadian/US NANP number -> E.164 (+1XXXXXXXXXX), for wa.me links.
    None for anything that isn't a plausible 10-digit NANP number."""
    if not isinstance(raw, str):
        return None
    d = re.sub(r"[^\d]", "", raw)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return "+1" + d
    return None


def to_listing(entry: dict) -> Listing:
    """Map one Kijiji Apollo-cache entity (``StandardListing`` or
    ``RealEstateListing``, "Room Rentals & Roommates" category) to our
    Listing. Every key is treated as optional so schema drift degrades
    fields rather than crashing — same policy as ``adapter.to_listing``."""
    loc = entry.get("location") or {}
    coords = loc.get("coordinates") or {}
    price = _price_native(entry)
    title = entry.get("title") or ""
    description = entry.get("description") or ""
    return Listing(
        id="kj" + str(entry.get("id", "")),
        category="sharing",
        title=title,
        url=entry.get("url") or "",
        price_eur=price,
        beds=None,
        baths=None,
        property_type=_classify_property_type(title, description),
        area=None,
        county=None,
        lat=coords.get("latitude"),
        lng=coords.get("longitude"),
        raw=entry,
        source="kijiji",
        currency="CAD",
        country="Canada",
        price_native=price,
        first_published=(entry.get("activationDate") or "")[:10] or None,
        room_type=None,
        preferences=_classify_gender_pref(title, description),
        # city stays None here, same as daft's to_listing() — the runner
        # resolves it from the search name via geo.city_of() and persists it
        # through store.set_cities_and_distances().
    )


def parse_detail(entry: dict) -> dict:
    """Pure map of a Kijiji detail-page entity to the enrichment fields
    ``store.apply_detail`` expects. Kijiji doesn't structure "sharing with" /
    "preferences" the way daft's propertyOverview does (or none was found in
    the pages inspected while building this) — those stay None; the
    dashboard already renders "—" for missing fields."""
    description = (entry.get("description") or "").strip()[:1000] or None
    last_updated = (entry.get("sortingDate") or entry.get("activationDate") or "")[:10] or None
    poster = entry.get("posterInfo") or {}
    return {
        "sharing_with": None,
        "rooms_available": None,
        "preferences": None,
        "owner_occupied": None,
        "available_from": None,
        "bathroom_type": None,
        "description": description,
        "last_updated": last_updated,
        "agent_phone": _norm_phone_ca(poster.get("phoneNumber")),
        "agent_name": None,
    }


def _entity_id_from_url(url: str) -> str | None:
    """Kijiji ad URLs end in .../<slug>/<numeric-id> — pull the id back out
    so the detail page's Apollo cache (keyed by that same id) can be found."""
    m = re.search(r"/(\d+)(?:[/?].*)?$", url)
    return m.group(1) if m else None


class KijijiListingsAdapter(SearchAdapter):
    """Adapter over kijiji.ca's "Room Rentals & Roommates" category
    (__NEXT_DATA__ / Apollo cache), fetched with a plain HTTP GET — no
    headless browser, kijiji.ca has no Cloudflare-style gate.

    Only page 1 (~40 listings, newest first — kijiji.ca sorts its search by
    DATE DESC) is fetched: the site paginates via a client-side XHR after
    hydration, which a plain HTTP GET can't drive, and at a 30-minute
    polling cadence the newest-first page 1 is enough to catch new listings
    and price changes without a browser.
    """

    def __init__(
        self,
        rate_limit_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
        fetch_html: Callable[[str], str] = _fetch_html,
    ):
        self._rate = rate_limit_seconds
        self._sleep = sleeper
        self._fetch_html = fetch_html

    def _with_backoff(self, op: str, fn: Callable[[], Any]) -> Any:
        for delay in _BACKOFF:
            try:
                return fn()
            except RateLimited:
                self._sleep(delay)
                continue
            except Exception as exc:  # noqa: BLE001
                raise AdapterError(f"kijiji.ca {op} failed: {exc!r}") from exc
        raise AdapterError(f"rate-limited by kijiji.ca ({op}); backoff exhausted")

    def _entries(self, html: str) -> list[dict]:
        data = _extract_next_data(html)
        try:
            apollo = data["props"]["pageProps"]["__APOLLO_STATE__"]
        except (KeyError, TypeError) as exc:
            raise AdapterError(f"unexpected kijiji.ca page shape: {exc!r}") from exc
        return [
            v
            for k, v in apollo.items()
            if (k.startswith("StandardListing:") or k.startswith("RealEstateListing:"))
            and v.get("categoryId") == _CATEGORY_ID
        ]

    def fetch(self, search: Search) -> list[Listing]:
        location_id = search.params.get("location_id")
        if not location_id:
            raise AdapterError(f"search {search.name!r} has no params.location_id")
        url = _search_url(location_id)

        def _get() -> str:
            return self._fetch_html(url)

        html = self._with_backoff(f"search {search.name}", _get)
        entries = self._entries(html)
        return [to_listing(e) for e in entries]

    def detail(self, url: str) -> dict | None:
        eid = _entity_id_from_url(url)

        def _get() -> str:
            return self._fetch_html(url)

        html = self._with_backoff(f"detail {url}", _get)
        data = _extract_next_data(html)
        try:
            apollo = data["props"]["pageProps"]["__APOLLO_STATE__"]
        except (KeyError, TypeError) as exc:
            raise AdapterError(f"unexpected kijiji.ca detail page shape: {exc!r}") from exc
        for key in (f"StandardListing:{eid}", f"RealEstateListing:{eid}"):
            if key in apollo:
                return apollo[key]
        # ad was delisted between the search that surfaced it and this fetch
        return None

    def close(self) -> None:
        pass
