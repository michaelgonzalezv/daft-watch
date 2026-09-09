"""Publish listings as ``listings.json`` and commit it into a git checkout.

``listings.json`` is the frozen contract consumed by the caleta.tech rentals
dashboard. ``to_record`` defines exactly which keys land in that file.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import date

from .models import Listing

_log = logging.getLogger("daftwatch")


def to_record(l: Listing) -> dict:
    """Project a :class:`Listing` onto the frozen dashboard record.

    Exactly 25 keys. ``distance_centre_km`` comes from
    ``l.distances_km.get("centre")`` (float or ``None``); every other key is the
    same-named ``Listing`` attribute. ``owner_occupied`` stays bool/None and
    ``description`` stays str/None.
    """
    return {
        "id": l.id,
        "source": l.source,
        "currency": l.currency,
        "url": l.url,
        "title": l.title,
        "price_eur": l.price_eur,
        "price_native": l.price_native,
        "price_weekly": l.price_weekly,
        "beds": l.beds,
        "room_type": l.room_type,
        "sharing_with": l.sharing_with,
        "rooms_available": l.rooms_available,
        "preferences": l.preferences,
        "owner_occupied": l.owner_occupied,
        "available_from": l.available_from,
        "bathroom_type": l.bathroom_type,
        "property_type": l.property_type,
        "city": l.city,
        "area": l.area,
        "lat": l.lat,
        "lng": l.lng,
        "distance_centre_km": l.distances_km.get("centre"),
        "first_published": l.first_published,
        "last_updated": l.last_updated,
        "description": l.description,
    }


def _date_desc_key(iso: str | None) -> int:
    """Sort key making newer ISO dates sort first; ``None`` sorts last."""
    if not iso:
        return 1  # after every negated ordinal (all large negatives)
    try:
        return -date.fromisoformat(iso).toordinal()
    except ValueError:
        return 1


def write_json(path: str, listings: list[Listing], generated_at: str) -> None:
    """Atomically write ``listings.json`` to *path*.

    Content: ``{"generated_at", "count", "listings": [...]}`` where ``listings``
    is sorted by ``price_eur`` ascending, then most-recent ``first_published``
    first. Parent directories are created. The write goes to ``path + ".tmp"``
    then ``os.replace`` swaps it into place, so a reader never sees a partial
    file.
    """
    ordered = sorted(
        listings, key=lambda l: (l.price_eur, _date_desc_key(l.first_published))
    )
    payload = {
        "generated_at": generated_at,
        "count": len(ordered),
        "listings": [to_record(l) for l in ordered],
    }
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def git_publish(repo_dir: str, file_rel: str, message: str, push: bool) -> bool:
    """Stage, commit and optionally push *file_rel* inside *repo_dir*.

    Returns ``True`` only when a commit was actually made. When staging shows no
    change against HEAD, nothing is committed and it returns ``False`` (no empty
    commits).

    Note on ``push=True``: the commit happens *before* the push. If the commit
    succeeds but the push fails (e.g. no remote configured), this returns
    ``False`` but the local commit still stands.

    Never raises: any ``subprocess.CalledProcessError`` / ``FileNotFoundError``
    / ``OSError`` is logged at ERROR on the ``daftwatch`` logger and ``False`` is
    returned.
    """
    try:
        subprocess.run(
            ["git", "-C", repo_dir, "add", file_rel],
            check=True, capture_output=True,
        )
        if subprocess.run(
            ["git", "-C", repo_dir, "diff", "--cached", "--quiet"]
        ).returncode == 0:
            return False
        subprocess.run(
            ["git", "-C", repo_dir, "commit", "-m", message],
            check=True, capture_output=True,
        )
        if push:
            subprocess.run(
                ["git", "-C", repo_dir, "push"],
                check=True, capture_output=True,
            )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        _log.error("git_publish failed: %s", exc)
        return False
