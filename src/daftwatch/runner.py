from __future__ import annotations

import logging
from dataclasses import dataclass, field

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
            logger.error("search %r failed: %r", search.name, exc)
            result.searches_failed.append(search.name)
            result.adapter_broken = True
            continue
        fetched.extend(listings)
        store.sync(search.name, listings)

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
