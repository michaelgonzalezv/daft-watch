"""Fetch the user's watchlist (the ♥ favourites) from the dashboard's API.

The dashboard stores favourites in Redis behind ``/api/favs`` guarded by an
``x-fav-key`` header. The scraper reads the same list so the email digest can
carry a "watched room updated / back on the market" section that ignores the
normal price and distance filters.
"""

from __future__ import annotations

import json
import logging
import urllib.request

_log = logging.getLogger("daftwatch")


def fetch_watchlist(api_url: str | None, key: str | None, timeout: float = 15.0) -> set[str]:
    """Return the set of watched listing ids. Best-effort: any failure logs a
    warning and returns an empty set, so a network blip never blocks a cycle."""
    if not api_url or not key:
        return set()
    try:
        req = urllib.request.Request(api_url, headers={"x-fav-key": key})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {str(x) for x in (data.get("ids") or [])}
    except Exception as exc:  # noqa: BLE001 - deliberately swallow everything
        _log.warning("watchlist fetch failed: %s", exc)
        return set()
