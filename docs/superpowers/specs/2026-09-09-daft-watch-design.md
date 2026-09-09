# daft-watch — Design

Date: 2026-09-09
Status: Draft for review

## Purpose

A personal tool that periodically checks daft.ie rental and house-share
listings for a set of saved searches, stores them in a local database with
history, and sends the user an email when something relevant appears or
changes (new listing, price drop, listing removed).

Scope is personal use. daft.ie's terms prohibit automated scraping for
commercial use or redistribution; this tool is a single-user watcher that
runs at a low request rate with an honest, identifiable user agent. It is
not for resale or bulk data collection.

## Non-goals

- Sale listings, new homes, commercial property, land (only `rent` and
  `sharing`).
- A web UI or dashboard.
- Multi-user support or hosted service.
- Reverse-engineering daft.ie's private API by hand (a maintained library
  is used instead, isolated behind an adapter).

## Approach

Python application. The `daftlistings` library (version-pinned) is used as
the fetch layer, wrapped behind a `SearchAdapter` interface so that if the
library breaks against a daft.ie change, only one class needs rewriting.
Runs as a Docker container with an internal scheduling loop. State lives in
SQLite on a mounted volume. Alerts are sent by email via `smtplib` from the
standard library.

Two alternatives were considered and rejected:

- **Own HTTP client against daft.ie's gateway.** Removes the fragile
  dependency but moves the reverse-engineering burden (rotating API keys,
  headers) onto this project permanently. More ongoing work.
- **Headless browser (Playwright).** Survives an API lockdown but produces a
  ~1.2GB image, runs slowly, is fragile against HTML and anti-bot changes,
  and uses far more resources.

The chosen approach is the middle ground: no self-maintained
reverse-engineering, and the adapter isolation limits the blast radius of a
library break to roughly 80 lines of code.

## Architecture

```
daft-watch/
  config.yaml              # saved searches, filters (deferred), interval
  .env.example             # SMTP credentials template
  src/daftwatch/
    adapter.py             # SearchAdapter interface + DaftListingsAdapter
    models.py              # Listing dataclass
    store.py               # SQLite: upsert, diff vs previous snapshot -> events
    filters.py             # apply config rules to a list of Listing
    notify.py              # EmailNotifier (smtplib + STARTTLS), digest template
    runner.py              # orchestrates one cycle
    __main__.py            # CLI: `run` (one-shot), `loop` (interval)
  Dockerfile
  docker-compose.yml       # SMTP env, ./data volume for daft.db
  tests/
  README.md
```

### Components

- **`adapter.py`** — `SearchAdapter` is an abstract interface with one
  method, `fetch(search) -> list[Listing]`. `DaftListingsAdapter` implements
  it by translating a search's `params` into a `daftlistings` query,
  paginating until results are exhausted, and mapping each result to a
  `Listing`. It owns rate limiting (see below). Depends on: `daftlistings`,
  `models`.

- **`models.py`** — `Listing` dataclass: `id`, `category`, `title`, `url`,
  `price_eur` (normalized to monthly), `beds`, `baths`, `property_type`,
  `area`, `county`, `lat`, `lng`, `raw` (the source payload dict). No
  dependencies.

- **`store.py`** — Owns the SQLite database. `sync(search_name, listings)`
  upserts the current snapshot for one search and returns the list of
  `Event`s produced by the diff. `pending_events()` returns events not yet
  in `notified`. `mark_notified(event_ids)` records a successful send.
  Depends on: `sqlite3` (stdlib), `models`.

- **`filters.py`** — `apply(listings, filter_config) -> list[Listing]`.
  Pure function. For now the config is empty and it passes everything
  through. Depends on: `models`.

- **`notify.py`** — `EmailNotifier.send(events)` renders one digest email
  for a batch of events and sends it over SMTP with STARTTLS. Depends on:
  `smtplib`, `email` (stdlib).

- **`runner.py`** — `run_cycle()`: for each search, call the adapter, pass
  results to `store.sync`, collect events across all searches, filter them,
  then hand `store.pending_events()` (intersected with the filtered set) to
  the notifier, and `mark_notified` on success. `loop()`: call `run_cycle`,
  sleep `interval_minutes`, repeat; touch the heartbeat file each cycle.
  Depends on: every other module.

- **`__main__.py`** — argparse CLI. `python -m daftwatch run` does one
  cycle and exits; `python -m daftwatch loop` runs forever. Both load
  `config.yaml` and env vars first.

### Data flow per cycle

1. `runner` iterates `config.searches`.
2. For each search, `adapter.fetch` returns the current listings.
3. `store.sync` upserts them and returns `Event`s (NEW, PRICE_DROP,
   PRICE_UP, GONE, BACK).
4. Events from all searches are collected, then `filters.apply` drops
   listings that do not match.
5. `store.pending_events()` minus already-notified gives the send set,
   restricted to the filtered listings and to `notify.min_event_types`.
6. `notify.send` sends one email for the whole batch.
7. On success, `store.mark_notified` records the event ids.

## Data model

```sql
listings(
  id TEXT PRIMARY KEY,        -- daft listing id
  category TEXT,              -- rent | sharing
  title TEXT, url TEXT,
  price_eur INTEGER,          -- normalized to monthly (weekly * 52 / 12)
  beds INTEGER, baths INTEGER,
  property_type TEXT,
  area TEXT, county TEXT, lat REAL, lng REAL,
  raw_json TEXT,              -- raw payload, for debugging schema drift
  first_seen TEXT, last_seen TEXT,
  active INTEGER,             -- 1 visible, 0 disappeared
  missing_cycles INTEGER      -- consecutive cycles absent from all its searches
)

events(
  id INTEGER PRIMARY KEY,
  listing_id TEXT,
  type TEXT,                  -- NEW | PRICE_DROP | PRICE_UP | GONE | BACK
  old_price INTEGER, new_price INTEGER,
  detected_at TEXT
)

notified(
  event_id INTEGER PRIMARY KEY,
  sent_at TEXT
)
```

### Diff rules

Applied per cycle. A listing "belongs to" any search whose results included
it this cycle.

- Listing id not in `listings` → `NEW` event, insert with `active=1`,
  `missing_cycles=0`.
- Listing id present, `price_eur` differs → `PRICE_DROP` or `PRICE_UP`
  event, update price.
- Listing id present and seen this cycle → set `missing_cycles=0`,
  update `last_seen`.
- Listing id in `listings` with `active=1` but absent from the results of
  all its searches → increment `missing_cycles`. When `missing_cycles`
  reaches `gone_after_cycles` (default 2) → `GONE` event, set `active=0`.
  The threshold guards against false positives from pagination gaps or a
  transient fetch failure.
- Listing id with `active=0` reappears → `BACK` event, set `active=1`,
  `missing_cycles=0`.

### Price normalization

daft.ie lists some rentals per week and some per month. All prices are
normalized to monthly (`weekly * 52 / 12`, rounded) before storage and
comparison so that price-change detection compares like with like. The
original value is preserved in `raw_json`.

### Idempotency

Only events without a row in `notified` are sent. Rows are added only after
a successful SMTP send. A crash or SMTP failure mid-send therefore causes a
retry on the next cycle, never a duplicate and never a lost alert.

## Configuration

`config.yaml`:

```yaml
interval_minutes: 30
gone_after_cycles: 2
rate_limit_seconds: 2
searches:
  - name: "Dublin rent"
    category: rent
    params: { location: [dublin-city], min_beds: 1, max_price: 2200 }
  - name: "Sharing D6/D8"
    category: sharing
    params: { location: [dublin-6-dublin, dublin-8-dublin], max_price: 900 }
filters:            # deferred - passes everything for now
  keywords_exclude: []
notify:
  min_event_types: [NEW, PRICE_DROP, GONE]
```

Credentials come from environment variables only, never the YAML:
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `ALERT_FROM`,
`ALERT_TO`. `docker-compose.yml` loads them from a `.env` file
(git-ignored). `.env.example` documents them.

The `params` block is passed to the adapter, which maps its keys to
`daftlistings` query calls. The README documents how to find `location`
slugs (they appear in daft.ie search URLs).

## Fetch behavior

- `DaftListingsAdapter` builds a `daftlistings` search from `params` and
  paginates until results are exhausted.
- Rate limit: one request every `rate_limit_seconds` (default 2), with
  ±0.5s jitter.
- User agent is honest and identifiable (not spoofing a browser).
- Each cycle's raw results are stored in `listings.raw_json` for debugging.

## Error handling

- Network or parse failure on one search → log at WARN, skip that search
  for this cycle, do not abort the cycle, do not touch `missing_cycles`
  for that search's listings (the `gone_after_cycles` threshold absorbs
  the gap).
- `daftlistings` raises an unexpected exception (API changed) → log at
  ERROR with traceback, send a "scraper broken" email to `ALERT_TO` (at
  most once per 6 hours), keep the process alive.
- SMTP failure → events stay unmarked in `notified`, retried next cycle.
- daft.ie returns 429 or 403 → exponential backoff (30s → 5min cap) and,
  if it persists past the cap for several cycles, a warning email.

## Testing

TDD: tests are written before implementation for each module.

- **`store.py`** — unit tests against an in-memory SQLite database, one per
  transition (NEW, PRICE_DROP, PRICE_UP, GONE after N cycles, BACK) plus
  `notified` idempotency.
- **`filters.py`** — table-driven input→expected cases.
- **`notify.py`** — mocked SMTP; asserts one batch produces one email and
  checks the rendered digest.
- **`adapter.py`** — `daftlistings` mocked with real JSON fixtures captured
  from 2–3 result pages. One optional `@pytest.mark.live` test that hits
  the real site, excluded from CI.
- **`runner.py`** — integration test with a fake adapter, full cycle
  end-to-end against a temporary database.

## Docker

- Base image `python:3.12-slim`, dependencies installed with `pip`,
  runs as a non-root user.
- Entrypoint: `python -m daftwatch loop`.
- `docker-compose.yml`: volume `./data:/data` (database and logs),
  `env_file: .env`, `restart: unless-stopped`.
- Healthcheck: each cycle touches `/data/heartbeat`; the healthcheck
  reports unhealthy if that file is older than `2 * interval_minutes`.

## Deliverables

1. Repository with the structure above, code, and passing tests.
2. `README.md`: setup, `.env.example`, how to add searches, how to find
   daft.ie `location` slugs.
3. A working example `config.yaml`.

## Deferred

- Filter definition (price bands, bedroom counts, areas, keyword
  include/exclude, new-only vs. also price drops). The `filters` module and
  config key exist from the start; the rules are decided later.

## Open questions

None outstanding. Filters are explicitly deferred, not unresolved.
