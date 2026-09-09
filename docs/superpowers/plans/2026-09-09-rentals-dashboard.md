# rentals — sharing pipeline + dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Extend daft-watch to watch daft.ie house-shares in Cork/Limerick/Dublin, enrich each ≤€800 candidate with detail-page fields ("Sharing with", preferences, …), email a curated HTML table, and publish `listings.json` to `caleta-web` powering `caleta.tech/tools/rentals`.

**Architecture:** See `docs/superpowers/specs/2026-09-09-rentals-dashboard-design.md` — read it fully before starting. Two repos: `daft-watch` (this repo, tasks 1–10) and `caleta-web` at `D:/Github/caleta-web` (tasks 11–12). Windows Task Scheduler runs `python -m daftwatch run` every 30 min; the scraper does scrape → detail-fetch → SQLite → `listings.json` → `git push` to caleta-web → email.

**Tech Stack:** Python 3.12, Playwright (already in), stdlib `sqlite3`/`smtplib`/`email`/`subprocess`/`math`, `pytest`. Dashboard: vanilla HTML/CSS/JS, no build.

**Spec:** `docs/superpowers/specs/2026-09-09-rentals-dashboard-design.md`

## Global Constraints

- Python 3.12; source under `src/daftwatch/`, tests under `tests/`.
- Data model is source-agnostic: `source` and `currency` are real fields; nothing new may hard-code EUR/Ireland except the daft adapter and the `geo.CENTRES` table.
- `Listing` stays a frozen dataclass. Adding fields: give every new field a default (`None` / `False` / `"daft"` / `{}`) so existing construction sites and tests keep working. Put mutable defaults behind `field(default_factory=...)`.
- SQLite: a real v1→v2 migration (ALTER TABLE + `PRAGMA user_version = 2`), guarded so it only runs when the stored `user_version < 2`. Never drop/recreate a table.
- All prices are integer, per-month, in the listing's own currency; `price_eur` remains the name the base code/tests use and for daft equals `price_native`.
- Playwright detail fetches reuse the existing `_default_client` browser/context and the `RateLimited` + `_page_with_backoff` machinery. One nav per detail page. Never fetch a detail page whose `detail_fetched` is already 1.
- `export.git_publish` must never raise into the cycle — log at ERROR and continue. No empty commits.
- Dashboard: match `caleta-web`'s design system — bootstrap the theme the same way `dashboards.html` does, reuse `/assets/site.css` and `/assets/theme.js`, same header/footer markup. English only, but honour light/dark.
- TDD: failing test first for every scraper task. Commit per task. Conventional commits, each ending:
  `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`
- Work on branch `feat/implementation` (continues PR #1) unless told otherwise.

---

### Task 1: `geo.py` — haversine + city centres

**Files:** Create `src/daftwatch/geo.py`, `tests/test_geo.py`

**Interfaces — Produces:**
- `haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float`
- `CENTRES: dict[str, tuple[float, float]]` — `{"cork": (51.8979, -8.4706), "limerick": (52.6633, -8.6267), "dublin": (53.3473, -6.2591)}`
- `city_of(slug_or_name: str) -> str | None` — lowercases, then returns the first CENTRES key that appears as a token in the string (`"dublin-city"`/`"Dublin sharing <=800"` → `"dublin"`), else `None`.
- `distance_to_centre(lat: float | None, lng: float | None, city: str | None) -> float | None` — `haversine_km` to `CENTRES[city]`; `None` if any input missing/unknown.

- [ ] **Step 1: failing tests**

```python
import math
from daftwatch.geo import haversine_km, city_of, distance_to_centre, CENTRES

def test_haversine_zero():
    assert haversine_km(53.0, -6.0, 53.0, -6.0) == 0.0

def test_haversine_known():
    # O'Connell Bridge Dublin -> Grand Parade Cork ~ 219 km
    d = haversine_km(53.3473, -6.2591, 51.8979, -8.4706)
    assert 210 < d < 230

def test_city_of():
    assert city_of("dublin-city") == "dublin"
    assert city_of("Cork sharing <=800") == "cork"
    assert city_of("limerick-city-2") == "limerick"
    assert city_of("galway") is None

def test_distance_to_centre():
    d = distance_to_centre(51.9020, -8.4765, "cork")
    assert d is not None and d < 1.0
    assert distance_to_centre(None, -8.0, "cork") is None
    assert distance_to_centre(51.9, -8.4, "galway") is None
```

- [ ] **Step 2:** run → fail (no module). **Step 3:** implement:

```python
from __future__ import annotations
import math

CENTRES: dict[str, tuple[float, float]] = {
    "cork": (51.8979, -8.4706),
    "limerick": (52.6633, -8.6267),
    "dublin": (53.3473, -6.2591),
}

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(min(1.0, math.sqrt(a)))

def city_of(slug_or_name: str) -> str | None:
    s = (slug_or_name or "").lower()
    for key in CENTRES:
        if key in s:
            return key
    return None

def distance_to_centre(lat, lng, city):
    if lat is None or lng is None or city not in CENTRES:
        return None
    clat, clng = CENTRES[city]
    return round(haversine_km(lat, lng, clat, clng), 2)
```

- [ ] **Step 4:** tests pass. **Step 5:** commit `feat(geo): haversine + city centres`.

---

### Task 2: extend the `Listing` model

**Files:** Modify `src/daftwatch/models.py`, `tests/test_models.py`

**Interfaces — Produces:** `Listing` gains, all with defaults:
`source: str = "daft"`, `currency: str = "EUR"`, `price_native: int = 0`,
`price_weekly: int | None = None`, `first_published: str | None = None`,
`last_updated: str | None = None`, `sharing_with: int | None = None`,
`rooms_available: int | None = None`, `preferences: str | None = None`,
`owner_occupied: bool | None = None`, `available_from: str | None = None`,
`bathroom_type: str | None = None`, `description: str | None = None`,
`room_type: str | None = None`,
`distances_km: dict[str, float] = field(default_factory=dict)`,
`detail_fetched: bool = False`.
Plus `parse_price_range(text: str) -> int` — like `parse_price` but for
`"From €725 to €750 per month"` returns the **low** end (725); delegates to
`parse_price` for everything else. And `parse_int(text) -> int | None` (first
integer in a string, else None) for the `propertyOverview` values.

- [ ] **Step 1: failing tests** — new fields default correctly on a minimal
  `Listing(...)`; `Listing` still frozen; `parse_price_range("From €725 to €750 per month") == 725`;
  `parse_price_range("€160 per week") == round(160*52/12)`;
  `parse_int("Sharing with 3 people") == 3`, `parse_int("n/a") is None`.

- [ ] **Step 2–4:** implement (add fields after the existing ones; keep field
  order so positional construction in old tests still works — all new fields
  come last and have defaults). Run `pytest tests/test_models.py -v` and
  `pytest -q`.

- [ ] **Step 5:** commit `feat(models): source-agnostic + share detail fields`.

---

### Task 3: store v1→v2 migration + detail bookkeeping

**Files:** Modify `src/daftwatch/store.py`, `tests/test_store.py`

**Interfaces — Consumes:** the extended `Listing`. **Produces:**
- `listings` table gains a column per new scalar `Listing` field (TEXT/INTEGER;
  bools as 0/1; `sharing_with`/`rooms_available` INTEGER) plus `detail_json TEXT`
  (holds `distances_km` and any future dict/list), `detail_fetched INTEGER NOT NULL DEFAULT 0`.
- Schema for a fresh DB creates them directly and sets `PRAGMA user_version = 2`.
- On an existing DB: if `user_version < 2`, `ALTER TABLE listings ADD COLUMN ...`
  each new column (wrapped so a re-run is safe), then `PRAGMA user_version = 2`.
- `sync(...)` persists the new scalar fields from the `Listing` and
  `json.dumps({"distances_km": l.distances_km})` into `detail_json`.
- `needs_detail(price_cap: int, limit: int | None = None) -> list[str]` —
  ids where `active = 1 AND detail_fetched = 0 AND price_eur > 0 AND price_eur <= price_cap`,
  oldest `first_seen` first, optionally capped at `limit`.
- `apply_detail(listing_id: str, fields: dict) -> None` — UPDATE the detail
  columns (`sharing_with`, `rooms_available`, `preferences`, `owner_occupied`,
  `available_from`, `bathroom_type`, `description`, `last_updated`), set
  `detail_fetched = 1`.
- `set_distances(listing_id: str, distances: dict[str, float]) -> None` — merge
  into `detail_json`.
- `get_listing` / new `active_listings() -> list[Listing]` reconstruct the full
  `Listing` including detail fields and `distances_km` from `detail_json`.

- [ ] **Step 1: failing tests**
  - fresh DB → `PRAGMA user_version` is 2, new columns exist.
  - **migration**: build a v1 DB by running the *old* `SCHEMA` string (inline a
    minimal v1 schema in the test), insert one row, open with `Store`, assert
    `user_version == 2`, the row still there, new columns present and NULL/0.
  - `needs_detail`: 3 listings (800, 1200, 750), cap 800 → returns the 800 and
    750 ids only; after `apply_detail` on one, it drops out; `limit=1` caps.
  - `apply_detail` round-trips via `get_listing` (`sharing_with`, `preferences`).
  - `set_distances` then `get_listing().distances_km == {...}`.
  - `active_listings` returns only `active=1`, fully hydrated.

- [ ] **Step 2–4:** implement. Migration helper:

```python
_V2_COLUMNS = [
    ("source", "TEXT DEFAULT 'daft'"), ("currency", "TEXT DEFAULT 'EUR'"),
    ("price_native", "INTEGER DEFAULT 0"), ("price_weekly", "INTEGER"),
    ("first_published", "TEXT"), ("last_updated", "TEXT"),
    ("sharing_with", "INTEGER"), ("rooms_available", "INTEGER"),
    ("preferences", "TEXT"), ("owner_occupied", "INTEGER"),
    ("available_from", "TEXT"), ("bathroom_type", "TEXT"),
    ("description", "TEXT"), ("room_type", "TEXT"),
    ("detail_json", "TEXT"), ("detail_fetched", "INTEGER NOT NULL DEFAULT 0"),
]

def _migrate(db):
    v = db.execute("PRAGMA user_version").fetchone()[0]
    if v >= 2:
        return
    existing = {r[1] for r in db.execute("PRAGMA table_info(listings)")}
    for name, decl in _V2_COLUMNS:
        if name not in existing:
            db.execute(f"ALTER TABLE listings ADD COLUMN {name} {decl}")
    db.execute("PRAGMA user_version = 2")
    db.commit()
```

  Call `_migrate` in `__init__` right after `executescript(SCHEMA)` (SCHEMA now
  contains the v2 columns and `PRAGMA user_version = 2` for fresh DBs; `_migrate`
  is the no-op-or-upgrade path for existing ones).

- [ ] **Step 5:** commit `feat(store): v2 schema, migration, detail bookkeeping`.

---

### Task 4: adapter — detail fetch, enrich, new search-page fields

**Files:** Modify `src/daftwatch/adapter.py`, `tests/test_adapter.py`; create `tests/fixtures/daft_share_detail.json`, `tests/fixtures/daft_share_search.json`

**Interfaces — Consumes:** `Listing`, `geo` (no — geo is wired in the runner). **Produces:**
- `to_listing(d, category)` also sets: `source="daft"`, `currency="EUR"`,
  `price_native = price_eur`, `room_type = d.get("numBedrooms")` when
  `category == "sharing"` (else keep beds parsing), `price_weekly` = the weekly
  int when `d["price"]` contains "per week" else None, `first_published` = ISO
  from `d.get("publishDate")` (ms epoch → `date.isoformat()`).
  Use `parse_price_range` for `price_eur`/`price_native` (handles "From X to Y").
  For `sharing`, `beds` stays `None` (the share's `numBedrooms` is a room type,
  not a count) — put it in `room_type` instead.
- `_default_client.detail(path: str) -> dict` — `self._ensure()`, `new_page`,
  `goto(urljoin(_DAFT_BASE, path), wait_until="domcontentloaded")`,
  `wait_for_selector("script#__NEXT_DATA__", state="attached")`, `content()`,
  `_extract_next_data` → `data["props"]["pageProps"]["listing"]` (LOUD KeyError).
  Also flatten `propertyOverview`: return the listing dict with an extra key
  `_overview = {item["label"].strip().lower(): item["text"].strip() for item in listing.get("propertyOverview", [])}`.
- `parse_detail(listing_dict: dict) -> dict` — pure; from the detail listing
  dict (with `_overview`) produce `{"sharing_with": parse_int(ov.get("sharing with")),
  "rooms_available": parse_int(ov.get("bedrooms available")),
  "preferences": ov.get("preferences") or None,
  "owner_occupied": _yn(ov.get("owner occupied")),
  "available_from": ov.get("available from") or None,
  "bathroom_type": listing_dict.get("bathroomType"),
  "description": (listing_dict.get("description") or "").strip()[:1000] or None,
  "last_updated": _iso(listing_dict.get("lastUpdateDate"))}` where `_yn("No") -> False`, `_yn("Yes") -> True`, else None.

- [ ] **Step 1:** capture the two fixtures — a real sharing search page and a
  real sharing detail page `__NEXT_DATA__` (the controller captured samples in
  `.superpowers/sdd/2026-09-09-daft-watch/sharing_capture.json` and
  `sharing_detail*.json` — reshape them into the
  `{"props":{"pageProps":{...}}}` form the code parses; trim `media`/`seller`
  blobs). If you can reach daft.ie, recapture live for freshness.

- [ ] **Step 2: failing tests**
  - `to_listing` on a sharing search item: `source=="daft"`, `room_type=="Single Room"`,
    `beds is None`, `price_weekly` set for a weekly one, `first_published` ISO.
  - `parse_detail` on the fixture: `sharing_with == 4`, `preferences == "Female"`,
    `owner_occupied is False`, `rooms_available == 1`, `description` non-empty & ≤1000.
  - `_default_client.detail` still not unit-tested directly (needs browser) —
    but factor `_extract_next_data` reuse so a `detail`-shaped html string is
    covered by an existing helper test.
  - `@pytest.mark.live`: real `detail()` on one path returns a dict with
    `propertyOverview` and `parse_detail` yields a non-empty `sharing_with` OR
    the field is legitimately absent (assert it does not raise).

- [ ] **Steps 3–4:** implement; run `pytest tests/test_adapter.py -v`, `pytest -q`.

- [ ] **Step 5:** commit `feat(adapter): sharing detail fetch + enrichment`.

---

### Task 5: filters — price cap, sharing-with, distance

**Files:** Modify `src/daftwatch/filters.py`, `tests/test_filters.py`

**Interfaces — Produces:**
- `apply(listings, filter_config)` also honours `max_price` (drop when
  `l.price_eur > max_price`), `max_sharing_with` (drop when
  `l.sharing_with is not None and l.sharing_with > max_sharing_with`).
  Keyword-exclude stays.
- `within_distance(listings, per_city_km: dict[str, float]) -> list[Listing]` —
  keep `l` when its `l.distances_km.get("centre")` is `None` (unknown → keep) or
  `<= per_city_km.get(city_key(l), inf)`. The listing carries its city via a new
  `Listing.source`? no — via `distances_km` only having "centre"; pass city in.
  **Simpler:** `within_distance(listings, limits_by_city, city_getter)` where
  `city_getter(l) -> str | None`. The runner passes `lambda l: geo.city_of(l.area or "")` —
  but `area` is None for daft. **Decision:** store the resolved city on the
  listing. Add `Listing.city: str | None = None` in Task 2 (amend) — the runner
  sets it from the search that found the listing. Then
  `within_distance(listings, limits)` keeps `l` when
  `l.city not in limits or l.distances_km.get("centre", 0) <= limits[l.city]`.

  > **Amendment to Task 2:** also add `city: str | None = None` to `Listing` and
  > a `city` column in Task 3's `_V2_COLUMNS` (`("city", "TEXT")`), persisted by
  > `sync` and read by `get_listing`/`active_listings`.

- [ ] **Steps 1–5:** table-driven tests for each; `within_distance` with a
  listing in `dublin` at 3km vs limit 6 (keep) and 8km vs 6 (drop) and unknown
  city (keep). Commit `feat(filters): price cap, sharing-with, distance`.

---

### Task 6: `export.py` — JSON + git publish

**Files:** Create `src/daftwatch/export.py`, `tests/test_export.py`

**Interfaces — Produces:**
- `to_record(l: Listing) -> dict` — every field the dashboard needs:
  `id, source, currency, url, title, price_eur, price_native, price_weekly,
  beds, room_type, sharing_with, rooms_available, preferences, owner_occupied,
  available_from, bathroom_type, property_type, city, area, lat, lng,
  distance_centre_km (= distances_km.get("centre")), first_published,
  last_updated, description`.
- `write_json(path, listings, generated_at: str) -> None` — atomic (write
  `path + ".tmp"` then `os.replace`); content
  `{"generated_at", "count", "listings": [to_record(l) ...]}`; `listings`
  sorted price asc then `first_published` desc for a stable diff. Creates parent
  dirs.
- `git_publish(repo_dir, file_rel, message, push: bool) -> bool` — returns True
  if it committed. `subprocess.run(["git","-C",repo_dir,"add",file_rel], check=True)`;
  `if subprocess.run(["git","-C",repo_dir,"diff","--cached","--quiet"]).returncode == 0: return False`;
  commit; `if push: git push`. Any `CalledProcessError`/`FileNotFoundError` →
  `logging.getLogger("daftwatch").error(...)`, return False. **Never raises.**

- [ ] **Step 1: failing tests**
  - `write_json` writes valid JSON, `count` matches, no `.tmp` left, dates ISO,
    sorted.
  - `git_publish`: `tmp_path` with `git init` + a committed base; write a file,
    `git_publish(..., push=False)` → returns True, `git log` shows the commit;
    call again unchanged → returns False, no new commit; a bogus `repo_dir` →
    returns False, no raise.

- [ ] **Steps 2–5:** implement; commit `feat(export): listings.json + git publish`.

---

### Task 7: config — new keys

**Files:** Modify `src/daftwatch/config.py`, `tests/test_config.py`, `config.example.yaml`

**Interfaces — Produces:** `Config` gains
`detail_price_cap: int = 800`, `detail_max_per_cycle: int = 60`,
`publish: PublishConfig | None`, `email_distance_km: dict[str, float] = {}`.
`PublishConfig` dataclass: `json_path: str`, `repo_dir: str`, `file_rel: str`,
`git_push: bool = True`. `load_config` parses `publish:` (None if absent) and
`email: { distance_km: {...} }`. `category` validation now also accepts the
existing `rent`/`sharing` (unchanged).

- [ ] Tests: a config with the full `publish`/`email` blocks parses; without
  them → `publish is None`, `email_distance_km == {}`, caps default. Update
  `config.example.yaml` to the spec's example (3 sharing searches, publish,
  email.distance_km). Commit `feat(config): publish + email distance keys`.

---

### Task 8: notify — HTML table digest

**Files:** Modify `src/daftwatch/notify.py`, `tests/test_notify.py`

**Interfaces — Produces:** `send_digest(items)` builds
`EmailMessage` with `msg.set_content(<plaintext, as today>)` then
`msg.add_alternative(<html>, subtype="html")`. HTML: a heading line, then a
`<table>` with a header row and one `<tr>` per `(event, listing)` — columns
**Price** (`€{price_native}/mo`, plus `<br><small>€{price_weekly}/wk</small>`
when set), **Rooms** (`rooms_available`), **Sharing** (`sharing_with`),
**Prefs** (`preferences or "—"`), **Type** (`property_type`), **Area**
(`area or city or "—"`), **km** (`distance_centre_km`), **Published**
(`first_published`), **Link** (`<a href=url>view</a>`). Inline styles only
(`style="..."` on `<table>`/`<td>`), border-collapse, ~13px, a muted header
row. Rows in the order given. Subject format unchanged.

- [ ] Tests: `msg.get_body("html")` exists; the HTML contains `<table`, one
  `<tr>` per item (+1 header), a known price string, a listing URL; plain-text
  body still present and unchanged in shape. Mocked SMTP unchanged. Commit
  `feat(notify): HTML table digest`.

---

### Task 9: runner — detail loop, distances, export, publish, curated digest

**Files:** Modify `src/daftwatch/runner.py`, `tests/test_runner.py`

**Interfaces — Consumes:** everything above + `geo`, `export`.
**Produces:** `run_cycle` unchanged signature; new behaviour after
`finish_cycle` (or the guarded skip) and before notify:

```
# 1. resolve city per synced listing and persist distance
for l in <all listings synced this cycle>:
    city = geo.city_of(<the search name that returned it>)   # track during the fetch loop
    dist = geo.distance_to_centre(l.lat, l.lng, city)
    store.set_city(l.id, city)               # add tiny store helper, or fold into sync
    if dist is not None:
        store.set_distances(l.id, {"centre": dist})

# 2. detail-fetch new cheap candidates
for lid in store.needs_detail(config.detail_price_cap, config.detail_max_per_cycle):
    try:
        detail = adapter_detail(lid)         # via client.detail(path) — see below
    except RateLimited:
        <backoff once, then break the detail loop for this cycle>
    except AdapterError:
        logger.exception("detail fetch failed for %s", lid); continue
    store.apply_detail(lid, parse_detail(detail))

# 3. export + publish
export.write_json(config.publish.json_path, store.active_listings(), now_iso)
export.git_publish(config.publish.repo_dir, config.publish.file_rel,
                   f"data: rentals listings {now_hhmm}", config.publish.git_push)
#   (all of step 3 skipped when config.publish is None)

# 4. curated digest
to_send = [(e, store.get_listing(e.listing_id)) for e in <pending & type-gated & filter-gated>]
to_send = [(e, l) for (e, l) in to_send if l]                       # existing
to_send = within_distance-filter on the listings, keeping event pairing
to_send.sort(key=lambda p: (p[1].price_eur, _neg_date(p[1].first_published)))
notifier.send_digest(to_send)  ... mark_notified as today
```

- The detail loop needs the adapter to expose detail fetching. Add to
  `DaftListingsAdapter` a method `fetch_detail(path: str) -> dict` that calls
  `self._make_client()`… **no** — reuse ONE client for the whole cycle. Refactor
  `fetch` and the new detail loop to share a client: `run_cycle` (or a small
  `AdapterSession`) creates the client once, passes it to `fetch` and to a new
  `adapter.detail_via(client, path)`. Simplest: give `DaftListingsAdapter` a
  `detail(path)` that lazily creates & reuses `self._client`, and a
  `close()` the runner calls in a `finally`. Keep `FakeClient` working: the
  runner tests pass a fake adapter with `fetch` + `detail` + `close`.
- `needs_detail` returns ids; the runner must turn an id into a path — store the
  `seoFriendlyPath`/`url` and derive the path (`url.replace(_DAFT_BASE, "")`),
  or add `store.detail_path(id)`.
- `city` tracking: the fetch loop already iterates `for search in config.searches`;
  keep a `{listing.id: geo.city_of(search.name)}` map as you go, use it in step 1.

- [ ] **Step 1: failing tests** (fake adapter returning canned search + detail):
  - full cycle: 2 shares (700 & 900) in "Cork sharing" → only the 700 gets a
    detail fetch (`needs_detail` cap 800); its `sharing_with` lands in the DB;
    `listings.json` is written (point `publish.json_path` at `tmp_path`);
    `git_publish` is called (assert via a fake/monkeypatch, or a real tmp git
    repo).
  - digest is distance-filtered: a 700 share 8km from Dublin centre with
    `email_distance_km={"dublin":6}` and city "dublin" → excluded from the
    email though present in `listings.json`.
  - digest sorted price asc.
  - `config.publish is None` → no JSON, no git, cycle still emails.
  - adapter `detail` raising `AdapterError` for one id → logged, other ids still
    processed, cycle completes.
  - `RateLimited` on detail → detail loop breaks, JSON/export still run, no crash.

- [ ] **Steps 2–4:** implement. `pytest tests/test_runner.py -v`, `pytest -q -W error`.

- [ ] **Step 5:** commit `feat(runner): detail loop, distances, export, curated digest`.

---

### Task 10: docs + example config + Task Scheduler

**Files:** Modify `README.md`; create `deploy/run-rentals.bat`, `deploy/DaftWatch-Rentals.xml`, `deploy/README.md`

**No code / no unit tests** — verification is manual.

- [ ] `deploy/run-rentals.bat`:

```bat
@echo off
cd /d D:\Github\daft-watch
call .venv\Scripts\activate 2>nul
for /f "usebackq tokens=1,* delims==" %%a in ("D:\Github\daft-watch\.env") do set "%%a=%%b"
python -m daftwatch run --config data\config.yaml --db data\daft.db >> data\rentals.log 2>&1
```

(adjust the venv line to how the user runs Python; if they use the global
interpreter, drop it.)

- [ ] `deploy/DaftWatch-Rentals.xml` — a Task Scheduler task importable via
  `schtasks /create /xml`, trigger: repeat every 30 minutes indefinitely,
  action: `run-rentals.bat`, "Run whether user is logged on or not" not set
  (simplest: only when logged on), start in `D:\Github\daft-watch`.

- [ ] `deploy/README.md` — how to import the task
  (`schtasks /create /tn "DaftWatch Rentals" /xml deploy\DaftWatch-Rentals.xml`),
  how to change the interval, where the log is, how the first run takes ~20–30
  min (detail-fetch warm-up) so run `run-rentals.bat` once by hand first.

- [ ] `README.md` — new "Rentals sharing pipeline" section: what it does, the
  `config.yaml` shape (point at `config.example.yaml`), the `publish:` block
  (must point `repo_dir` at a real `caleta-web` checkout with push rights),
  the Task Scheduler deploy, and that Docker is now secondary.

- [ ] **Commit** `docs: rentals pipeline + Task Scheduler deploy`.

---

### Task 11: dashboard page in `caleta-web`

**Files (in `D:/Github/caleta-web`):** Create `tools/rentals/index.html`,
`tools/rentals/app.css`, `tools/rentals/app.js`, `tools/rentals/listings.json`
(sample, ~8 rows so the page renders pre-first-run)

**Work in the `caleta-web` repo** (`git -C D:/Github/caleta-web`), on a branch
`feat/rentals-tool`. Read `dashboards.html`, `tools/mt5-analyzer/index.html`,
and `assets/site.css` first to match head/header/footer/theme exactly.

**Interfaces — Produces:** a static page at `caleta.tech/tools/rentals`:
- `index.html` — the site `<head>` conventions (fonts preload, `/assets/site.css?v=2`,
  theme bootstrap `<script>`, canonical `https://www.caleta.tech/tools/rentals`,
  OG/twitter tags, favicon), the shared `<header>` nav markup with
  `aria-current` off (it is not a top-nav item) and the shared `<footer>`, then
  `<main class="container">` holding `<div id="filters">` and
  `<table id="grid">`. Loads `/assets/theme.js?v=1` and `./app.js` (defer).
  **No** `i18n.js` (English only). **No** build step.
- `app.css` — uses `site.css` custom properties; a filter bar (flex-wrap),
  range sliders, a dense sortable table (`th[aria-sort]`, click to sort),
  right-aligned numeric cells, a muted "updated <time>" caption, mobile: the
  table scrolls in an `overflow-x:auto` wrapper.
- `app.js` (vanilla, ~250–350 lines):
  - `fetch('./listings.json')` → render.
  - state object mirrored to `location.search` (read on load, write on change).
  - filters: max price (slider, max = data max), reference point (`<select>` of
    `Cork / Limerick / Dublin` centres + a text `<input>` "type an address" →
    on Enter geocode via
    `https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=…`,
    cache `{query: [lat,lng]}` in `localStorage`, show the resolved label),
    max distance km (slider), `Sharing with ≤` (number, default 4, empty = off),
    city (checkboxes), property type (checkboxes), source (checkboxes),
    published-within (`24h/3d/7d/14d/any`), preferences contains / excludes
    (text). Reset button.
  - distance: computed client-side with a local `haversine(aLat,aLng,bLat,bLng)`
    (copy the formula) from each row's `lat/lng` to the active reference point;
    shown in the "Distance" column and driving the km filter. If a row has no
    `lat/lng`, distance is blank and the km filter does not exclude it.
  - table: columns Price / Rooms / Sharing / Prefs / Type / Area / Distance /
    Published / Source / Link. Header click sorts (toggle asc/desc, single
    key); default sort price asc then published desc. Price cell shows
    `{currency} {price_native}/mo` and, when `price_weekly`, a `<small>` /wk.
  - empty state ("no listings match") and a load-error state ("couldn't load
    listings.json").
- `listings.json` — a small hand-made sample matching `export.to_record`'s
  shape exactly (copy the field list from Task 6), ~8 rows across the 3 cities
  with varied `sharing_with`/`preferences`/weekly-vs-monthly so every filter is
  demonstrable. This file is overwritten by the scraper in production.

- [ ] **Step 1:** read the 3 reference files, note the exact head/header/footer.
- [ ] **Step 2:** write `listings.json` sample (contract-locked to Task 6).
- [ ] **Step 3:** write `index.html` + `app.css` + `app.js`.
- [ ] **Step 4:** open `tools/rentals/index.html` via a local static server
  (`python -m http.server` in `caleta-web`), exercise every filter + sort +
  the geocode box + theme toggle + mobile width. Fix issues. Screenshot-check
  light and dark.
- [ ] **Step 5:** commit (in `caleta-web`) `feat(rentals): listings dashboard at /tools/rentals`.

---

### Task 12: link the tool into the site + Vercel header

**Files (in `caleta-web`):** Modify `tools.html`, `vercel.json`

- [ ] `tools.html` — add a new `<section class="theme">` "Housing" (unique
  `aria-controls="theme-housing"` / `id`), one real `.dash-card`:
  `<h3><a href="/tools/rentals">Rentals — Ireland shares</a></h3>`, a
  `<p>` describing it, `<a class="dash-open" href="/tools/rentals">Open tool →</a>`,
  `<div class="dash-meta">Personal · daft.ie · updated every 30 min</div>`, and
  a `<span class="dash-tag">Personal</span>`. Start the section
  `aria-expanded="false"` and collapsed (copy the collapsed-section pattern from
  the file's own comment). Leave the count badge empty — `accordion.js` fills it.
- [ ] `vercel.json` — add to `headers`:

```json
{ "source": "/tools/rentals/listings.json",
  "headers": [ { "key": "Cache-Control", "value": "no-cache" } ] }
```

- [ ] Manual: `npx vercel dev` (or the site's local preview) — `/tools`
  shows the card, `/tools/rentals` loads, `listings.json` returns `no-cache`.
- [ ] **Commit** (in `caleta-web`) `chore(rentals): link tool in /tools, no-cache listings.json`.
- [ ] Open a PR in `caleta-web` (`feat/rentals-tool` → `main`); the daft-watch
  work stays on PR #1.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `Listing` new fields incl. `source`/`currency`/`city` | 2 (+5 amendment) |
| geo / haversine / centres | 1 |
| store v1→v2 migration, `needs_detail`, `apply_detail`, `active_listings` | 3 |
| adapter `detail()`, `parse_detail`, sharing search-page fields | 4 |
| filters: `max_price` strict, `max_sharing_with`, `within_distance` | 5 |
| `export.write_json` + `git_publish` (no empty commits, never raises) | 6 |
| config `publish` / `detail_*` / `email.distance_km` | 7 |
| HTML-table digest | 8 |
| runner: detail loop, distance persistence, export, publish, curated+sorted digest, `publish is None` path | 9 |
| Task Scheduler deploy, READMEs | 10 |
| `caleta.tech/tools/rentals` page, filters, sort, geocode, sample json | 11 |
| `/tools` card, `vercel.json` no-cache, caleta-web PR | 12 |
| source-agnostic for Kijiji | 2, 6, 11 (schema only; not built) |

**Placeholder scan:** the two adapter fixtures (Task 4) are "capture real or
reshape the controller's samples" — concrete, not deferred. Task 10's `.bat`
venv line is explicitly "adjust to how the user runs Python".

**Type consistency:** `Listing.city` added in Task 2 amendment + Task 3
`_V2_COLUMNS` + used in Task 5 `within_distance` and Task 6 `to_record` and
Task 9 runner. `distances_km` dict persisted as `detail_json` JSON in Task 3,
read back in `get_listing`/`active_listings`, surfaced as `distance_centre_km`
in Task 6. `detail(path)` on the adapter (Task 4) consumed by the runner
(Task 9) via a reused client + `close()`.

**Scope:** two repos, one plan — acceptable because the `caleta-web` half
(tasks 11–12) only consumes the `listings.json` contract frozen in Task 6 and
has its own branch + PR.
