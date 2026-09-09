# rentals — sharing-listing pipeline + caleta.tech dashboard

Date: 2026-09-09
Status: Draft for review
Builds on: 2026-09-09-daft-watch-design.md (the base scraper, already implemented and working)

## Purpose

Extend the daft-watch scraper to watch daft.ie **house-share** listings across
Cork, Limerick and Dublin, enrich each candidate with the "Sharing with"
count and other detail-page fields, and publish two outputs:

1. **A curated email** (HTML table) — new / price-dropped listings that also
   pass a per-city distance-to-centre filter.
2. **`listings.json`** committed to the `caleta-web` repo, powering a new
   interactive page at **`caleta.tech/tools/rentals`** where the user browses
   and filters everything.

The data model and the dashboard are **source-agnostic** from day one: Daft
is the first `source`; Kijiji (Canada) is planned next, so `currency` and
`source` are first-class fields and nothing is hard-coded to EUR or Ireland.

## Non-goals

- Kijiji itself (only the schema accommodates it now).
- A hosted database or API. `listings.json` is a static file on Vercel.
- Docker. Deployment is Windows Task Scheduler.
- Rental (non-share) listings — the base scraper's `rent` category stays but
  this work targets `sharing`.
- Geocoding on the server. The dashboard geocodes user-typed addresses in the
  browser (Nominatim).

## Deployment model

Windows Task Scheduler runs `python -m daftwatch run` every 30 minutes. One
cycle:

1. scrape the configured searches (daft.ie sharing, 3 cities);
2. cheap pre-filter on search-page data (price ≤ 800 strict, active);
3. for each **new** id that passed the pre-filter, load its detail page once
   and extract the enrichment fields; store them so they are never re-fetched;
4. upsert into SQLite, compute diff events (as today);
5. write `listings.json` to the path configured under `publish:`;
6. `git add / commit / push` that file in the `caleta-web` checkout (if
   `publish.git_push` is true);
7. send the email digest (HTML table) for new + price-drop events that pass
   the per-city distance filter.

Docker files stay in the repo but are no longer the recommended path; the
README's primary instructions become Task Scheduler.

## Data model changes

`Listing` (frozen dataclass) gains:

| field | type | source |
|---|---|---|
| `source` | `str` | constant `"daft"` for now |
| `currency` | `str` | `"EUR"` for daft |
| `price_native` | `int` | the listing's price in its own currency/period, normalized to per-month (same rule as `price_eur`); `price_eur` stays as the EUR-normalized alias so existing code/tests keep working, and for daft `price_eur == price_native` |
| `price_weekly` | `int \| None` | per-week figure when the listing was priced weekly, else `None` (for display) |
| `first_published` | `str \| None` | ISO date from `firstPublishDate` / `publishDate` |
| `last_updated` | `str \| None` | ISO date from `lastUpdateDate` |
| `sharing_with` | `int \| None` | from detail page `propertyOverview` "Sharing with" |
| `rooms_available` | `int \| None` | detail page "Bedrooms Available" |
| `preferences` | `str \| None` | detail page "Preferences" (e.g. "Female", "Students") |
| `owner_occupied` | `bool \| None` | detail page "Owner Occupied" |
| `available_from` | `str \| None` | detail page "Available From" |
| `bathroom_type` | `str \| None` | detail page `bathroomType` |
| `description` | `str \| None` | detail page `description` (plain text, trimmed to ~1000 chars) |
| `room_type` | `str \| None` | search-page `numBedrooms` for shares ("Single Room", "Double Room") |
| `distances_km` | `dict[str, float]` | straight-line km from this listing to each named reference point (see below); always includes the listing's own city centre |
| `detail_fetched` | `bool` | whether the detail page has been loaded (so we don't retry every cycle) |

SQLite `listings` table gains one column per scalar field above; `distances_km`
and any dict/list go in a `detail_json TEXT` column. A migration bumps
`PRAGMA user_version` to 2 and `ALTER TABLE`s the new columns onto an existing
v1 database (all nullable / defaulted, so old rows are fine).

`raw_json` finally carries real data (the base-scraper review's finding #6 —
carried in as part of this work).

### Reference points

Hard-coded city centres (lat, lng):

- Cork — Grand Parade `51.8979, -8.4706`
- Limerick — O'Connell Street `52.6633, -8.6267`
- Dublin — O'Connell Bridge `53.3473, -6.2591`

`distances_km` stores `{"centre": <km to own city centre>}`. The dashboard
recomputes distance to any point the user picks, client-side, from the
listing's stored `lat`/`lng` — so the stored value is only a default sort key
and the email filter input.

## Scraper changes (`daft-watch` repo)

### `adapter.py`

- `_build_url` already handles `sharing`. Keep.
- New `_default_client` method `detail(listing_id_or_path: str) -> dict` — one
  Playwright navigation to the listing's `seoFriendlyPath`, returns the parsed
  `props.pageProps.listing` dict (plus `propertyOverview` flattened to a
  `{label: text}` map). Raises `RateLimited` on a challenge page, same as
  `page()`.
- `to_listing` maps the search-page dict as today, plus sets `source="daft"`,
  `currency="EUR"`, `room_type`, `first_published`, `price_weekly`.
- New `enrich(listing: Listing, detail: dict) -> Listing` — returns a copy with
  the detail fields filled (`sharing_with`, `preferences`, `rooms_available`,
  `owner_occupied`, `available_from`, `bathroom_type`, `description`,
  `last_updated`, `detail_fetched=True`).
- `propertyOverview` label parsing: the labels seen are "Bedrooms Available",
  "Available From", "Available For", "Sharing with", "Owner Occupied",
  "Preferences". Match case-insensitively; "Sharing with" / "Owner Occupied"
  → int / bool, the rest → trimmed string.

### `geo.py` (new)

`haversine_km(lat1, lng1, lat2, lng2) -> float`, and `CENTRES: dict[str,
tuple[float, float]]` keyed by a city slug family. `city_of(search_name_or_slug)
-> str` maps a search's location slug to a centre key (`dublin-city` →
`dublin`, `cork-city` → `cork`, ...). Pure, fully unit-tested.

### `store.py`

- v1 → v2 migration (add columns, bump `user_version`).
- `sync` writes the new scalar fields and `detail_json`.
- New `needs_detail() -> list[str]` — ids of active listings with
  `detail_fetched = 0` and `price_eur <= <cap>` (the cap passed in). The runner
  uses this to decide which detail pages to load.
- New `apply_detail(listing_id, fields: dict)` — updates the detail columns and
  sets `detail_fetched = 1`.
- `get_listing` returns the full enriched `Listing`.
- `active_listings() -> list[Listing]` — every `active = 1` row, for the JSON
  export.

### `filters.py`

- `apply` gains `max_price` (strict, on `price_eur`) and
  `max_sharing_with` (drop when `sharing_with` is not None and exceeds it).
- New `within_distance(listings, per_city_km: dict[str, float]) -> list` — keep
  a listing when `distances_km["centre"] <= per_city_km[city]` (or when the
  city has no configured limit). Used by the **email path only**.

### `export.py` (new)

`write_json(path: str, listings: list[Listing], generated_at: str) -> None` —
writes `{"generated_at": ..., "count": N, "listings": [ {...} ]}` with every
field a dashboard needs, `lat`/`lng` included, prices as integers, dates as
ISO strings. Atomic write (temp file + rename).

`git_publish(repo_dir: str, file_rel: str, message: str, push: bool) -> None`
— `git -C <repo_dir> add <file_rel>`; if `git diff --cached --quiet` reports a
change, `commit` with `message` and, when `push`, `git push`. No-op when
nothing changed (so no empty commits). Uses `subprocess`; any git failure is
logged at ERROR and swallowed (a publish failure must not kill the cycle or
lose the email).

### `runner.py`

`run_cycle` after `finish_cycle`, before the notify step:

1. `ids = store.needs_detail(cap=config.detail_price_cap)` (default 800);
   optionally capped at `config.detail_max_per_cycle` (default 60).
2. for each id: `detail = adapter.detail(...)`; `store.apply_detail(id,
   parsed)`. Per-id `RateLimited` → back off (reuse `_page_with_backoff`
   shape) then skip that id this cycle, retry next.
3. compute `distances_km` for every synced listing (`geo`) and persist.
4. `export.write_json(config.publish.json_path, store.active_listings(), now)`.
5. `export.git_publish(config.publish.repo_dir, config.publish.file_rel,
   f"data: rentals listings {now:%Y-%m-%d %H:%M}", config.publish.git_push)`.
6. notify — but the digest set is first passed through
   `filters.within_distance(config.email.distance_km)` and sorted
   (price asc, then `first_published` desc).

The base-scraper ruling stands: if any search raised `AdapterError`, the GONE
sweep is still skipped that cycle; detail-fetch and JSON export still run for
what did sync.

### `notify.py`

- `send_digest` renders **`multipart/alternative`**: a plain-text part (as
  today) and an HTML part with a `<table>` — columns Price / /wk / Rooms /
  Sharing / Prefs / Area / km / Published / Link. One `<tr>` per event, rows
  ordered as the runner passed them. Keep the subject format.
- HTML is inline-styled (email clients ignore `<style>`), minimal, readable in
  light and dark clients.

### `config.py`

New keys:

```yaml
detail_price_cap: 800
detail_max_per_cycle: 60
publish:
  json_path: "D:/Github/caleta-web/tools/rentals/listings.json"
  repo_dir: "D:/Github/caleta-web"
  file_rel: "tools/rentals/listings.json"
  git_push: true
email:
  distance_km: { dublin: 6, cork: 4, limerick: 4 }
```

`searches` for this use case:

```yaml
searches:
  - name: "Cork sharing <=800"
    category: sharing
    params: { location: [cork-city], max_price: 800 }
  - name: "Limerick sharing <=800"
    category: sharing
    params: { location: [limerick-city], max_price: 800 }
  - name: "Dublin sharing <=800"
    category: sharing
    params: { location: [dublin-city], max_price: 800 }
```

`max_price` in `params` still drives the daft URL (coarse); `filters.max_price`
does the strict cut.

## Dashboard (`caleta-web` repo)

New folder `tools/rentals/`:

- `index.html` — matches the site: same `<head>` pattern as
  `tools/mt5-analyzer` / `dashboards.html` (fonts preload, `/assets/site.css`,
  theme bootstrap script, canonical `https://www.caleta.tech/tools/rentals`,
  OG tags), the shared `<header>` / `<footer>` markup, `theme.js`. **English
  only** (no `data-i18n`, it is a personal utility not site content — but it
  still honours the light/dark theme toggle).
- `app.js` — vanilla JS, no build step (unlike mt5-analyzer which is a Vite
  bundle; this is small enough to hand-write). Fetches `./listings.json`,
  renders a filter bar + a sortable table.
- `app.css` — scoped styles on top of `site.css` tokens (`--ink`, `--surface`,
  `--line`, `--gold`, `--radius`, …).

### Filters (all live, client-side)

- **Max price** (range slider, currency-aware label).
- **Distance**: a reference-point control — a dropdown of the city centres,
  plus a text input "or type an address"; on enter, geocode via
  `https://nominatim.openstreetmap.org/search?format=json&q=…` (1 req, cached
  in `localStorage`), then a **max-km slider**. Distance column + the slider
  both use `haversine(listing.lat/lng, refPoint)` computed in the browser.
- **Sharing with ≤ N** — number input, default `4`, blank = no limit. Applies
  only to rows that have the value.
- **City** (multi-select), **property type** (multi-select), **source**
  (multi-select — just "daft" today), **room type**.
- **Published within** — 24h / 3d / 7d / 14d / any.
- **Preferences** — text contains / exclude.
- A "reset" button. Filter state is serialized to the URL query string so a
  view can be bookmarked/shared.

### Table

Columns: Price (native, with /wk in a sub-line when weekly) · Rooms avail ·
Sharing with · Preferences · Type · Area · Distance (km, to the current ref
point) · Published · Source · a link out to the listing. Every column header
sorts (click to toggle asc/desc); default sort **price asc, then published
desc**. Row count + "updated <generated_at>" shown above the table.

No map in v1 (distance number + sort is enough); a later iteration can add a
Leaflet map if wanted.

### Wiring into the site

- `tools.html` — add a new theme section "Housing" (or a card under an
  existing one) with a `.dash-card` linking to `/tools/rentals`, tag
  "Personal · updated every 30 min".
- `vercel.json` — no change needed (`cleanUrls` already serves
  `/tools/rentals`). `listings.json` is served as a static asset;
  the existing `/assets/(.*)` `no-cache` header does not cover `/tools/...`,
  so add a header rule making `tools/rentals/listings.json` `no-cache` so the
  browser always gets the latest after a redeploy.

## Testing

Scraper (TDD, same discipline as the base):

- `geo.py` — haversine against known distances; `city_of` slug mapping.
- `adapter.detail` parsing — a captured real detail-page `__NEXT_DATA__`
  fixture (`tests/fixtures/daft_share_detail.json`), assert the
  `propertyOverview` flattening and `enrich`.
- `store` v1→v2 migration — open a v1 db, migrate, assert columns + data
  survive; `needs_detail` / `apply_detail` round-trip.
- `filters` — `max_price` strict, `max_sharing_with`, `within_distance`.
- `export.write_json` — shape, atomicity (temp file), ISO dates.
- `export.git_publish` — run against a throwaway `git init` repo in `tmp_path`:
  first call commits, second call with no change is a no-op, `push=False`
  never invokes push.
- `notify` — the HTML part exists, is a table, has one row per event, columns
  present; plain-text part unchanged.
- `runner` — fake adapter returning search + detail payloads: full cycle
  populates detail fields, writes JSON, calls `git_publish`, digest is
  distance-filtered and sorted.
- `@pytest.mark.live` — one real sharing search + one real detail fetch.

Dashboard: no framework, so a small `tools/rentals/app.test.html` is overkill;
instead a `tests/` note and manual acceptance (load with a sample
`listings.json`, exercise each filter). The scraper's `export` tests guarantee
the JSON contract the dashboard depends on.

## Deliverables

1. `daft-watch`: the scraper changes above, tests green, `listings.json`
   export + git publish working against a local `caleta-web` checkout.
2. `caleta-web`: `tools/rentals/{index.html,app.js,app.css}`, a sample
   `listings.json` committed so the page renders before the first real run,
   the `tools.html` card, the `vercel.json` header.
3. A Windows Task Scheduler `.xml` (import-ready) or a `run-rentals.bat` +
   step-by-step, running every 30 min.
4. READMEs updated in both repos.

## Open questions

- Tool name confirmed: `rentals` → `caleta.tech/tools/rentals`.
- Next source (Kijiji, Canada) — schema ready, not built.
- First run will detail-fetch every ≤800 share in 3 cities (~100–150 pages,
  ~20–30 min once). Mitigation: `detail_max_per_cycle` spreads it over a few
  cycles; the user can also let the first few 30-min runs warm the cache.
