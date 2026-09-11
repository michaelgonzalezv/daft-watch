from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from daftwatch import export, filters, geo
from daftwatch.adapter import (
    _DAFT_BASE,
    AdapterError,
    SearchAdapter,
    parse_detail,
)
from daftwatch.config import Config
from daftwatch.export import _date_desc_key
from daftwatch.fx import fetch_rates_usd
from daftwatch.kijiji_adapter import parse_detail as parse_kijiji_detail
from daftwatch.notify import EmailNotifier
from daftwatch.store import Store
from daftwatch.watchlist import fetch_watchlist


@dataclass
class CycleResult:
    events_sent: int = 0
    searches_failed: list[str] = field(default_factory=list)
    adapter_broken: bool = False
    # None when publishing is not configured; True/False = did this cycle's
    # export.write_json + git_publish succeed.
    publish_ok: bool | None = None


def run_cycle(
    config: Config,
    store: Store,
    adapter: SearchAdapter,
    notifier: EmailNotifier,
    logger: logging.Logger,
    fx_fetcher: Callable[[list[str]], dict[str, float]] | None = None,
) -> CycleResult:
    result = CycleResult()
    store.begin_cycle()

    fetched: list = []
    city_by_id: dict[str, str | None] = {}
    for search in config.searches:
        try:
            listings = adapter.fetch(search)
        except AdapterError as exc:
            logger.exception("search %r failed: %r", search.name, exc)
            result.searches_failed.append(search.name)
            result.adapter_broken = True
            continue
        city = geo.city_of(search.name)
        for l in listings:
            city_by_id[l.id] = city
        fetched.extend(listings)
        store.sync(search.name, listings)

    # finish_cycle (the GONE sweep) is skipped WHOLESALE when any search failed,
    # not just for that search's listings: the shipped schema has no
    # listing<->search table, so "a listing absent from all its searches" (the
    # spec's GONE rule) is unimplementable. We err toward never emitting a false
    # GONE; a sustained scraper stall is covered by the broken-scraper alert.
    if not result.adapter_broken:
        store.finish_cycle(config.gone_after_cycles)

    # 1. persist resolved city + centre distance for every synced listing, in
    #    ONE transaction. Skip a listing whose stored city and centre distance
    #    already match; use the Listing objects from this cycle's fetch rather
    #    than re-reading each row.
    fetched_by_id = {l.id: l for l in fetched}
    geo_updates: dict[str, tuple[str | None, float | None]] = {}
    for lid, city in city_by_id.items():
        src = fetched_by_id.get(lid)
        if src is None:
            continue
        dist = geo.distance_to_centre(src.lat, src.lng, city)
        current = store.get_listing(lid)
        if (
            current is not None
            and current.city == city
            and current.distances_km.get("centre") == dist
        ):
            continue
        geo_updates[lid] = (city, dist)
    if geo_updates:
        store.set_cities_and_distances(geo_updates)

    # 2. detail-fetch cheap, not-yet-enriched candidates (enriches the DB
    #    regardless of whether publishing is configured)
    consecutive_failures = 0
    for lid in store.needs_detail(config.detail_price_cap, config.detail_max_per_cycle):
        listing = store.get_listing(lid)
        url = (listing.url if listing else "") or ""
        src = listing.source if listing else "daft"
        # daft's detail() wants a path relative to its own base; every other
        # source's detail() takes the listing's full URL, which this leaves
        # untouched since it never starts with _DAFT_BASE.
        path = url[len(_DAFT_BASE):] if url.startswith(_DAFT_BASE) else url
        if not path:
            logger.warning("no url for listing %s; skipping detail fetch", lid)
            continue
        try:
            detail = adapter.detail(path)
        # rate limiting surfaces as AdapterError here (see adapter._with_backoff);
        # the 3-strike breaker below handles a sustained block.
        except AdapterError:
            logger.exception("detail fetch failed for %s", lid)
            consecutive_failures += 1
            if consecutive_failures >= 3:
                # A systemic Cloudflare block is not a per-listing problem, and
                # each AdapterError has already burned the full backoff ladder
                # (~750s). Stop before the loop overruns the cycle cadence.
                logger.warning(
                    "detail loop: 3 consecutive failures, stopping this cycle"
                )
                break
            continue
        consecutive_failures = 0
        if detail is None:
            # Delisted between the fetch that surfaced it and now. Mark it
            # enriched-with-nothing so needs_detail stops returning it; the
            # active/gone sweep drops it from listings.json within
            # gone_after_cycles.
            logger.info("listing %s has no detail page (delisted); skipping", lid)
            store.apply_detail(lid, {})
            continue
        parse_fn = parse_detail if src == "daft" else parse_kijiji_detail
        store.apply_detail(lid, parse_fn(detail))

    # 3. export listings.json (+ events.json) and commit them (publish only)
    if config.publish is not None:
        try:
            now = datetime.now(timezone.utc)
            # daft's rentalPrice_to URL param filters on the NATIVE (often
            # weekly) price, so price-ineligible listings come back. Apply the
            # monthly max_price / keyword cut to the JSON too — but NOT
            # max_sharing_with: the dashboard has its own adjustable house-size
            # control and wants every price-eligible listing.
            export_listings = filters.apply(
                store.export_listings(config.export_gone_within_days),
                {k: v for k, v in config.filters.items() if k != "max_sharing_with"},
            )
            # not a bound default (`= fetch_rates_usd`): resolving the bare
            # name here, at call time, off this module's globals lets tests
            # monkeypatch daftwatch.runner.fetch_rates_usd once instead of
            # threading a fake through every publish-exercising call site.
            fx_usd = (fx_fetcher or fetch_rates_usd)(
                sorted({l.currency for l in export_listings})
            )
            wrote = export.write_json(
                config.publish.json_path, export_listings, now.isoformat(),
                fx_usd=fx_usd,
            )

            rels = [config.publish.file_rel]
            if config.publish.events_path and config.publish.events_rel:
                history = store.events_history(config.events_history_days)
                if export.write_events_json(
                    config.publish.events_path, history, now.isoformat()
                ):
                    wrote = True
                rels.append(config.publish.events_rel)

            if config.publish.history_path and config.publish.history_rel:
                store.snapshot_prices(now.date().isoformat())
                rows = store.price_history_rows(config.price_history_days)
                if export.write_history_json(
                    config.publish.history_path, rows, now.isoformat()
                ):
                    wrote = True
                rels.append(config.publish.history_rel)

            if not wrote:
                # nothing changed on disk -> no commit needed, not a failure
                result.publish_ok = True
            else:
                result.publish_ok = export.git_publish(
                    config.publish.repo_dir,
                    rels,
                    f"data: rentals listings {now:%Y-%m-%d %H:%M}",
                    config.publish.git_push,
                )
        except Exception:
            logger.exception("export/publish failed")
            result.publish_ok = False

    # The email is deliberately narrower than the dashboard: on top of every
    # configured filter it also honours email.max_price (the dashboard shows
    # every price; the digest stays focused on affordable rooms). That cap is
    # authored in EUR, so comparing it against a listing in another currency
    # (e.g. Kijiji's CAD) would silently be wrong. Rather than let non-EUR
    # listings through uncapped — which is exactly what flooded a single
    # digest with ~700 rows the first time Kijiji's 19 regions were added,
    # since every one of them was "NEW" on that first sync — every currency
    # but EUR is excluded from the digest entirely until it has its own
    # configured threshold. GONE events are unaffected (they bypass this set
    # below regardless of source).
    email_filters = dict(config.filters)
    if config.email_max_price is not None:
        email_filters["max_price"] = config.email_max_price
    eur_fetched = [l for l in fetched if l.currency == "EUR"]
    allowed_ids = {l.id for l in filters.apply(eur_fetched, email_filters)}
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

    # 4. curate: drop what is too far from its city centre, sort price-asc
    #    then newest-first.
    if to_send:
        kept = {
            l.id
            for l in filters.within_distance(
                [l for _, l in to_send], config.email_distance_km
            )
        }
        to_send = [(e, l) for (e, l) in to_send if l.id in kept]
        to_send.sort(
            key=lambda p: (p[1].price_eur, _date_desc_key(p[1].first_published))
        )

    # 4b. watchlist: any change on a room the user is watching (♥ on the
    #     dashboard), regardless of the price / distance / min_event_types
    #     filters — this is the "the room I liked is available again" signal.
    watch_send: list[tuple] = []
    watched = fetch_watchlist(config.watchlist_api, config.watchlist_key)
    if watched:
        _WATCH_TYPES = {"NEW", "PRICE_DROP", "PRICE_UP", "BACK", "GONE"}
        in_normal = {l.id for _, l in to_send}
        for event in store.pending_events():
            if (
                event.listing_id not in watched
                or event.type not in _WATCH_TYPES
                or event.listing_id in in_normal
            ):
                continue
            listing = store.get_listing(event.listing_id)
            if listing is not None:
                watch_send.append((event, listing))
        watch_send.sort(
            key=lambda p: (p[1].price_eur, _date_desc_key(p[1].first_published))
        )

    sent_ids: list[int] = []
    if to_send or watch_send:
        try:
            notifier.send_digest(to_send, watchlist_items=watch_send)
        except Exception:
            logger.exception("send_digest failed; events stay pending")
            raise
        sent_ids = [e.id for e, _ in to_send] + [e.id for e, _ in watch_send]
        result.events_sent = len(to_send) + len(watch_send)

    # Resolve every pending event whose listing was part of this cycle's fetched
    # set: it was either sent above or intentionally dropped (wrong type, filter,
    # or distance). Leaving the dropped ones pending would flood a later email
    # when a filter/distance/min_event_types setting is widened. Events whose
    # listing was not seen this cycle stay pending — including GONE, whose
    # listing is by definition absent from seen_this_cycle().
    seen = store.seen_this_cycle()
    sent_set = set(sent_ids)
    suppressed_ids = [
        ev.id
        for ev in store.pending_events()
        if ev.id not in sent_set and ev.listing_id in seen
    ]
    store.mark_notified(sent_ids + suppressed_ids)

    # 5. once a day, commit a plain-SQL dump of the DB somewhere durable (the
    #    live DB is laptop-only and not in git). Never fails the cycle.
    if config.backup is not None:
        try:
            p = config.backup.sql_path
            stale = (not os.path.exists(p)) or (
                time.time() - os.path.getmtime(p)
                > config.backup.every_hours * 3600
            )
            if stale:
                store.dump_sql(p)
                rel = os.path.relpath(p, config.backup.repo_dir).replace(os.sep, "/")
                now = datetime.now(timezone.utc)
                export.git_publish(
                    config.backup.repo_dir,
                    [rel],
                    f"backup: db dump {now:%Y-%m-%d}",
                    config.backup.git_push,
                )
        except Exception:
            logger.exception("db backup failed (non-fatal)")

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
    fx_fetcher: Callable[[list[str]], dict[str, float]] | None = None,
) -> None:
    last_alert: float | None = None
    last_publish_alert: float | None = None
    publish_failures = 0
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            result = run_cycle(config, store, adapter, notifier, logger, fx_fetcher)
            if result.adapter_broken:
                now = clock()
                if last_alert is None or now - last_alert >= _ALERT_THROTTLE_SECONDS:
                    notifier.send_alert(
                        "scraper may be broken",
                        "Failed searches: " + ", ".join(result.searches_failed),
                    )
                    last_alert = now
            if result.publish_ok is True:
                publish_failures = 0
            elif result.publish_ok is False:
                publish_failures += 1
                if publish_failures >= 3:
                    now = clock()
                    if (
                        last_publish_alert is None
                        or now - last_publish_alert >= _ALERT_THROTTLE_SECONDS
                    ):
                        notifier.send_alert(
                            "rentals publish failing",
                            f"{publish_failures} consecutive export/publish "
                            "failures; the dashboard listings.json is not "
                            "updating (check the git checkout / remote).",
                        )
                        last_publish_alert = now
        except Exception:
            logger.exception("run_cycle raised; continuing after sleep")
        finally:
            # Tear the adapter's browser down between cycles so a hung Chromium
            # never persists; the next run_cycle lazily recreates it.
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

        if heartbeat_path:
            Path(heartbeat_path).write_text(
                datetime.now(timezone.utc).isoformat(), encoding="utf-8"
            )
        sleeper(config.interval_minutes * 60)
