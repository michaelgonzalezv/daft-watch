"""Append-only archive of closed listings.

Once a listing has been off the market for a while, its final state — every
feature we extracted, its description, and its price path — is written here
once and never touched again, one gzip'd JSONL file per calendar quarter
(``2026-Q3.jsonl.gz``). The live SQLite DB keeps the row too; this is the
durable, cheap-to-store, easy-to-analyse copy (one JSON object per line,
concatenated gzip members, so appending never rewrites what's already there).

The dashboard never reads this. It's for looking back: price trends per
feature, how long a kind of room lasts, what a favourite used to cost.

Agent name/phone are deliberately not archived — nothing about a price
history needs a person's contact details.
"""

from __future__ import annotations

import gzip
import json
import os
from collections import defaultdict
from datetime import datetime

from daftwatch.store import Store

# columns copied as-is from the listings row
_COPY = (
    "id", "source", "country", "city", "category", "currency", "price_native",
    "price_weekly", "property_type", "area", "county", "lat", "lng", "beds",
    "baths", "room_type", "sharing_with", "rooms_available", "preferences",
    "owner_occupied", "available_from", "bathroom_type", "title", "description",
    "url", "first_published", "first_seen", "last_seen", "last_updated",
)


def _closed_at(row: dict) -> str | None:
    """When it went off the market: the last GONE event, else last_seen."""
    gone = [e["at"] for e in row.get("events", []) if e["type"] == "GONE"]
    return max(gone) if gone else row.get("last_seen")


def build_record(row: dict) -> dict:
    """Project an ``archive_candidates`` row onto the archived shape."""
    rec = {k: row.get(k) for k in _COPY}
    if rec["owner_occupied"] is not None:
        rec["owner_occupied"] = bool(rec["owner_occupied"])
    rec["distance_centre_km"] = (row.get("distances_km") or {}).get("centre")
    rec["closed_at"] = _closed_at(row)
    # the price path: every NEW / PRICE_DROP / PRICE_UP / BACK / GONE we saw
    rec["events"] = row.get("events", [])
    return rec


def _quarter(iso: str | None) -> str:
    try:
        d = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        d = None
    if d is None:
        return "unknown"
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def archive_closed(store: Store, archive_dir: str, after_days: int) -> int:
    """Append every not-yet-archived listing that has been closed for more than
    *after_days* to its quarter's file, then mark it archived in the DB and
    blank its raw payload. Returns how many were archived.

    Files are written before the DB is marked, so a crash in between can
    duplicate a line in the archive on the next run but can never lose one —
    readers dedupe by (id, closed_at).
    """
    rows = store.archive_candidates(after_days)
    if not rows:
        return 0
    os.makedirs(archive_dir, exist_ok=True)
    by_quarter: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        rec = build_record(row)
        by_quarter[_quarter(rec["closed_at"])].append(rec)
    for quarter, recs in by_quarter.items():
        payload = "".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in recs
        ).encode("utf-8")
        # "ab" appends a NEW gzip member; concatenated members are one valid
        # stream, so earlier lines are never rewritten.
        with gzip.open(os.path.join(archive_dir, f"{quarter}.jsonl.gz"), "ab") as fh:
            fh.write(payload)
    store.mark_archived([r["id"] for r in rows])
    store.prune_raw_json()
    return len(rows)
