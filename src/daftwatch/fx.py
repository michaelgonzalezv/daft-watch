from __future__ import annotations

import json
import logging
import urllib.request

_log = logging.getLogger("daftwatch")

# Used only when the live fetch fails (and there's no better option) — rough
# and occasionally stale, but the dashboard only uses this for "which is
# cheaper" comparisons, not anything that needs to be exact.
DEFAULT_RATES_USD = {"EUR": 1.08, "CAD": 0.73, "USD": 1.0}

_TIMEOUT = 8.0


def fetch_rates_usd(
    currencies: list[str], timeout: float = _TIMEOUT
) -> dict[str, float]:
    """USD value of one unit of each currency in *currencies*, e.g.
    ``{"EUR": 1.08}``. Fetched fresh (frankfurter.app, ECB rates, no API key)
    every call — a couple of tiny requests per scrape cycle is nowhere near
    worth caching. Best-effort per currency: one that fails to fetch falls
    back to ``DEFAULT_RATES_USD``; "USD" itself is always 1.0, no request.

    A currency that fails AND has no fallback rate is left OUT of the returned
    dict rather than defaulted to 1.0 — parity with the dollar is a silently
    wrong number (800 GBP published as $800), while a missing key is already
    handled downstream: ``regression.compute_comparison`` drops those rows and
    the dashboard's ≈$ line skips them.
    """
    out: dict[str, float] = {}
    for cur in currencies:
        if cur == "USD":
            out[cur] = 1.0
            continue
        try:
            url = f"https://api.frankfurter.app/latest?from={cur}&to=USD"
            # some CDNs 403 the default "Python-urllib/x.y" user agent
            req = urllib.request.Request(url, headers={"User-Agent": "daft-watch/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out[cur] = float(data["rates"]["USD"])
        except Exception:
            fallback = DEFAULT_RATES_USD.get(cur)
            if fallback is None:
                _log.error(
                    "fx rate fetch failed for %s and no fallback rate is "
                    "configured; omitting it (listings in %s get no USD "
                    "figure this cycle)", cur, cur, exc_info=True
                )
                continue
            _log.warning(
                "fx rate fetch failed for %s; using fallback", cur, exc_info=True
            )
            out[cur] = fallback
    return out
