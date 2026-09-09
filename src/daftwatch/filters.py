from __future__ import annotations

from daftwatch.models import Listing


def apply(listings: list[Listing], filter_config: dict) -> list[Listing]:
    excludes = [w.lower() for w in (filter_config or {}).get("keywords_exclude", [])]
    if not excludes:
        return list(listings)
    out = []
    for l in listings:
        title = (l.title or "").lower()
        if any(word in title for word in excludes):
            continue
        out.append(l)
    return out
