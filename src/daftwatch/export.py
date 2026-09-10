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

    Exactly 26 keys. ``distance_centre_km`` comes from
    ``l.distances_km.get("centre")`` (float or ``None``); every other key is the
    same-named ``Listing`` attribute. ``owner_occupied`` stays bool/None and
    ``description`` stays str/None.
    """
    return {
        "id": l.id,
        "source": l.source,
        "currency": l.currency,
        "country": l.country,
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


def write_json(path: str, listings: list[Listing], generated_at: str) -> bool:
    """Atomically write ``listings.json`` to *path*; return whether it wrote.

    Content: ``{"generated_at", "count", "listings": [...]}`` where ``listings``
    is sorted by ``price_eur`` ascending, then most-recent ``first_published``
    first. Parent directories are created. The write goes to ``path + ".tmp"``
    then ``os.replace`` swaps it into place, so a reader never sees a partial
    file.

    Skip-when-unchanged: if *path* already holds a file whose ``listings`` array
    is byte-for-byte the same records (same order) as this call would produce,
    nothing is written and ``False`` is returned. ``generated_at`` and ``count``
    are ignored in that comparison — only a real listing change rewrites the
    file (and so triggers a downstream commit / redeploy). Returns ``True`` when
    the file was written.
    """
    ordered = sorted(
        listings, key=lambda l: (l.price_eur, _date_desc_key(l.first_published))
    )
    records = [to_record(l) for l in ordered]
    payload = {
        "generated_at": generated_at,
        "count": len(ordered),
        "listings": records,
    }

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            # round-trip the new records through JSON so the comparison matches
            # what is actually on disk (e.g. ``default=str`` coercions).
            new_records = json.loads(json.dumps(records, default=str))
            if existing.get("listings") == new_records:
                return False
        except (OSError, ValueError, AttributeError):
            pass  # unreadable / malformed -> treat as changed, rewrite

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False, default=str)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return True


def git_publish(repo_dir: str, file_rel: str, message: str, push: bool) -> bool:
    """Stage, commit and optionally push *file_rel* inside *repo_dir*.

    Returns ``True`` only when a commit was actually made. When staging shows no
    change against HEAD, nothing is committed and it returns ``False`` (no empty
    commits).

    Staging, the no-op check and the commit are all scoped to *file_rel* with a
    ``-- <pathspec>`` so a commit here never sweeps in unrelated changes the
    user has staged elsewhere in *repo_dir*.

    Before committing, ``git pull --rebase --autostash`` is attempted so a
    remote that has moved ahead does not wedge every future push behind a
    non-fast-forward. A failed pull is logged at WARNING and the commit/push is
    still attempted.

    Note on ``push=True``: the commit happens *before* the push. If the commit
    succeeds but the push fails (e.g. no remote configured), this returns
    ``False`` but the local commit still stands.

    Never raises: any ``subprocess.CalledProcessError`` / ``FileNotFoundError``
    / ``OSError`` is logged at ERROR on the ``daftwatch`` logger and ``False`` is
    returned.
    """
    try:
        subprocess.run(
            ["git", "-C", repo_dir, "add", "--", file_rel],
            check=True, capture_output=True,
        )
        if subprocess.run(
            ["git", "-C", repo_dir, "diff", "--cached", "--quiet", "--", file_rel]
        ).returncode == 0:
            return False
        pull = subprocess.run(
            ["git", "-C", repo_dir, "pull", "--rebase", "--autostash"],
            capture_output=True,
        )
        if pull.returncode != 0:
            _log.warning(
                "git_publish: pull --rebase failed (continuing): %s",
                pull.stderr.decode("utf-8", "replace").strip(),
            )
        subprocess.run(
            ["git", "-C", repo_dir, "commit", "-m", message, "--", file_rel],
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
