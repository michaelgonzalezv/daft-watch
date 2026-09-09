from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from daftwatch import filters
from daftwatch.adapter import AdapterError, SearchAdapter
from daftwatch.config import Config
from daftwatch.notify import EmailNotifier
from daftwatch.store import Store


@dataclass
class CycleResult:
    events_sent: int = 0
    searches_failed: list[str] = field(default_factory=list)
    adapter_broken: bool = False


def run_cycle(
    config: Config,
    store: Store,
    adapter: SearchAdapter,
    notifier: EmailNotifier,
    logger: logging.Logger,
) -> CycleResult:
    result = CycleResult()
    store.begin_cycle()

    fetched: list = []
    for search in config.searches:
        try:
            listings = adapter.fetch(search)
        except AdapterError as exc:
            logger.exception("search %r failed: %r", search.name, exc)
            result.searches_failed.append(search.name)
            result.adapter_broken = True
            continue
        fetched.extend(listings)
        store.sync(search.name, listings)

    # finish_cycle (the GONE sweep) is skipped WHOLESALE when any search failed,
    # not just for that search's listings: the shipped schema has no
    # listing<->search table, so "a listing absent from all its searches" (the
    # spec's GONE rule) is unimplementable. We err toward never emitting a false
    # GONE; a sustained scraper stall is covered by the broken-scraper alert.
    if not result.adapter_broken:
        store.finish_cycle(config.gone_after_cycles)

    allowed_ids = {l.id for l in filters.apply(fetched, config.filters)}
    min_types = set(config.notify.min_event_types)

    to_send: list[tuple] = []
    for event in store.pending_events():
        if event.type not in min_types:
            continue
        if event.type != "GONE" and event.listing_id not in allowed_ids:
            continue
        listing = store.get_listing(event.listing_id)
        if listing is None:
            continue
        to_send.append((event, listing))

    if to_send:
        try:
            notifier.send_digest(to_send)
        except Exception:
            logger.exception("send_digest failed; events stay pending")
            raise
        store.mark_notified([e.id for e, _ in to_send])
        result.events_sent = len(to_send)

    return result


_ALERT_THROTTLE_SECONDS = 6 * 3600


def loop(
    config: Config,
    store: Store,
    adapter: SearchAdapter,
    notifier: EmailNotifier,
    logger: logging.Logger,
    *,
    heartbeat_path: str | None = None,
    sleeper=time.sleep,
    clock=time.monotonic,
    max_cycles: int | None = None,
) -> None:
    last_alert: float | None = None
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            result = run_cycle(config, store, adapter, notifier, logger)
            if result.adapter_broken:
                now = clock()
                if last_alert is None or now - last_alert >= _ALERT_THROTTLE_SECONDS:
                    notifier.send_alert(
                        "scraper may be broken",
                        "Failed searches: " + ", ".join(result.searches_failed),
                    )
                    last_alert = now
        except Exception:
            logger.exception("run_cycle raised; continuing after sleep")

        if heartbeat_path:
            Path(heartbeat_path).write_text(
                datetime.now(timezone.utc).isoformat(), encoding="utf-8"
            )
        sleeper(config.interval_minutes * 60)
