# daft-watch

Personal watcher for daft.ie rental and house-share listings. Polls saved
searches on an interval, stores results in SQLite with history, and emails
you when a listing is new, drops in price, or disappears.

Personal use only. It runs at one request every ~2 seconds with an honest
user agent. Do not use it for bulk data collection or resale — daft.ie's
terms forbid that.

## Setup

1. `cp .env.example .env` and fill in your SMTP details. For Gmail, enable
   2-factor auth and create an **App Password** (Google Account → Security →
   App passwords); use that as `SMTP_PASS`.
2. `mkdir -p data && cp config.example.yaml data/config.yaml`, then edit
   `data/config.yaml`.

   On Linux the container writes to `./data` as your host user. Either run
   `chmod -R a+rwX data` so it stays writable, or `export UID GID` before
   `docker compose up` so the compose `user:` mapping matches your host
   uid/gid.
3. `docker compose up -d --build`
4. `docker compose logs -f` to watch it.

## Configuring searches

Each entry under `searches`:

```yaml
- name: "label for your reference"
  category: rent          # rent | sharing
  params:
    location: [dublin-city, dublin-8-dublin]   # daft.ie URL slugs
    min_beds: 1
    max_beds: 3
    min_price: 800
    max_price: 2200
```

### Finding location slugs

Do a search on daft.ie in your browser and read the URL. For
`https://www.daft.ie/property-for-rent/dublin-8-dublin` the slug is
`dublin-8-dublin`. For a whole county:
`https://www.daft.ie/property-for-rent/dublin` → `dublin`. Add as many
slugs to the `location` list as you want.

## Filters

`filters.keywords_exclude` drops any listing whose title contains one of
the given words (case-insensitive):

```yaml
filters:
  keywords_exclude: [student, "short term", "rent a room"]
```

More filter rules are planned; for now every listing that matches your
daft.ie search parameters is considered.

## Notifications

`notify.min_event_types` controls which changes trigger an email. Options:
`NEW`, `PRICE_DROP`, `PRICE_UP`, `GONE`, `BACK`. One email is sent per
cycle summarising all changes.

## Running without Docker

```bash
pip install -r requirements.txt
pip install --no-deps .
export $(grep -v '^#' .env | xargs)
python -m daftwatch run --config config.yaml --db data/daft.db      # one cycle
python -m daftwatch loop --config config.yaml --db data/daft.db --heartbeat data/heartbeat   # forever
```

## Development

```bash
pip install -r requirements.txt
pytest -v
pytest -v -m live      # optional: hits the real site
```

## If it stops finding listings

The fetch path is `curl_cffi` (a Chrome TLS fingerprint, `impersonate="chrome131"`)
fetching daft.ie's normal search HTML pages and reading the listings out of the
page's `<script id="__NEXT_DATA__">` JSON blob (`props.pageProps.listings` /
`.paging`). The old `daftlistings` gateway API is Cloudflare-blocked (403) and no
longer used.

Only `src/daftwatch/adapter.py` (`_default_client` / `_extract_next_data` /
`_build_url`) knows these details — that is the single place to fix. If daft.ie
tightens its Cloudflare config, bump the `impersonate=` target (`chrome131` → a
newer profile) or adjust the `__NEXT_DATA__` selector. A Cloudflare "Security
Check" page is treated as transient (backoff + retry), not a broken scraper. You
will also get a "scraper may be broken" email (at most once per 6 hours) when a
search errors for real (e.g. the JSON shape changed).

HTTP calls (daft.ie and SMTP) carry a 30s timeout, so a network stall
recovers on the next cycle. Note that `restart: unless-stopped` only
restarts a container that *exits* — it does not act on the heartbeat, which
is informational unless you add an autoheal sidecar.
