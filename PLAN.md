# MTG Card Catalog — Implementation Plan

A local-only Python web app for tracking a personal MTG collection: SQLite-backed,
FastAPI + Jinja2 + HTMX UI, Manabox CSV import, Scryfall enrichment, TCGCSV daily prices.

## 0. Confirmed decisions

- **Stack:** FastAPI + Jinja2 + HTMX. Python-only, no JS build step.
- **CSS:** Tailwind via the standalone binary (one compiled `static/app.css`, ~30KB). Not CDN — CDN Tailwind ships ~3MB of JS that recomputes utilities at runtime. The standalone binary is a single Go executable with no Node dependency, so the "no JS build" constraint holds.
- **Scheduler:** None. Price refresh is a manual button that launches a background job; HTMX polls a progress endpoint.
- **Auth:** None. Bind to `127.0.0.1`. Single-user. API keys plaintext in a `settings` table.
- **DB:** SQLAlchemy 2.x (sync) + Alembic, SQLite with `journal_mode=WAL`, `foreign_keys=ON`, `synchronous=NORMAL` set via a connect-event listener.
- **Deps:** uv (`uv add`, `uv run`, `uv lock`). macOS host.
- **HTMX:** vendored locally (one ~14KB file), not CDN — offline-friendly.

## 1. Top-level architecture

```
                            HTMX requests (partials)
   Browser  <---HTML/CSS--->  FastAPI app (uvicorn, 127.0.0.1)
                                    |
        +---------------------------+--------------------------+
        |                |                  |                  |
     routers/         services/         clients/             db/
   (HTTP layer)   (business logic)   (external APIs)   (SQLAlchemy)
        |                |                  |                  |
        |          jobs/ (BG tasks)     scryfall.py            session, models
        |          progress registry    tcgcsv.py              Alembic migrations
        |                |              manabox_csv.py
        +-------- templates/ (Jinja2 + HTMX partials) ---------+
                         static/ (compiled tailwind, htmx.min.js, sparkline.js)
                         data/   (catalog.db, image cache, bulk-data snapshots)
```

Layering rule: **routers are thin** (parse query params, call a service, render a template).
**Services own transactions and orchestration.** **Clients own HTTP/CSV parsing**, no DB awareness.
**Jobs** are services launched on a thread / `BackgroundTasks` with progress reported through
an in-process `JobRegistry` keyed by UUID.

## 2. Directory layout

```
card_catalog/
├── pyproject.toml                # uv-managed: fastapi, jinja2, sqlalchemy, alembic, httpx, ijson, pydantic-settings
├── uv.lock
├── README.md
├── .env.example                  # SCRYFALL_USER_AGENT, DB_PATH
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/                 # numbered migration files
├── data/                         # gitignored
│   ├── catalog.db
│   ├── images/                   # optional local PNG cache, hashed paths
│   └── bulk/                     # scryfall bulk-data + tcgcsv archive downloads
├── static/
│   ├── app.css                   # tailwind compiled output, checked in
│   ├── htmx.min.js               # vendored, not CDN
│   └── sparkline.js              # ~30 lines, SVG path generator
├── templates/
│   ├── base.html
│   ├── partials/
│   │   ├── card_row.html         # one collection row, used by table & bulk-edit
│   │   ├── card_table.html       # full table, target for sort/page/filter swaps
│   │   ├── filter_panel.html
│   │   ├── job_status.html       # polled by HTMX to render progress bar
│   │   └── import_preview.html
│   ├── collection.html           # main browse page
│   ├── card_detail.html          # single card view with image + sparkline
│   ├── import.html               # upload + preview + commit
│   ├── prices.html               # refresh button, job history
│   └── settings.html
├── tailwind.config.js            # content: ["templates/**/*.html"]
├── input.css                     # @tailwind base/components/utilities
└── src/card_catalog/
    ├── __init__.py
    ├── main.py                   # FastAPI app factory, mount static, register routers
    ├── config.py                 # pydantic-settings: DB_PATH, USER_AGENT, SCRYFALL_DELAY_MS, default condition, currency
    ├── db/
    │   ├── __init__.py
    │   ├── session.py            # engine, SessionLocal, get_db dep, PRAGMA hook
    │   └── models.py             # all SQLAlchemy models (single file is fine at this scale)
    ├── routers/
    │   ├── collection.py         # GET /, /cards, /cards/{id}, POST /cards/{id}/edit, bulk-edit
    │   ├── imports.py            # GET /import, POST /import/preview, POST /import/commit
    │   ├── prices.py             # GET /prices, POST /prices/refresh, GET /jobs/{id}
    │   ├── archidekt.py          # GET /archidekt/export.csv, GET/POST /archidekt/reconcile
    │   ├── settings.py           # GET/POST /settings
    │   └── api.py                # tiny JSON endpoints if/when needed (autocomplete)
    ├── services/
    │   ├── collection.py         # query builder for filter/sort/page; edit/bulk-edit
    │   ├── import_manabox.py     # parse CSV, dry-run preview, commit with upsert
    │   ├── enrich_scryfall.py    # resolve cards, populate scryfall_cards, bulk-data sync
    │   ├── prices.py             # tcgcsv download + upsert + stats compute
    │   ├── archidekt.py          # export filtered collection to Archidekt CSV; deck reconcile
    │   └── settings.py
    ├── clients/
    │   ├── __init__.py
    │   ├── scryfall.py           # rate-limited httpx client; get_by_set_number, get_collection, download_bulk
    │   ├── tcgcsv.py             # download_group_csv, download_group_prices_json, list_groups
    │   ├── manabox_csv.py        # csv.DictReader with header normalization, row->dataclass
    │   └── archidekt.py          # CSV exporter + pyrchidekt wrapper (deck fetch for reconciliation)
    ├── jobs/
    │   ├── __init__.py
    │   ├── registry.py           # in-memory dict[uuid -> JobState]; thread-safe via Lock
    │   └── runner.py             # helper: launch BackgroundTask, capture exceptions, update progress
    └── domain/
        ├── enums.py              # Condition, Finish, Language enums
        └── identity.py           # canonical card-key normalization (set_code+collector_number, scryfall_id)
```

`models.py` stays a single file deliberately — at ~6 tables it doesn't earn a package.

## 3. Database schema

Two clean halves. **`scryfall_cards`** is a cache of canonical printing data, owned by the
Scryfall sync job, never written by user actions. **`collection_entries`** is the user's
data, with a FK to `scryfall_cards`.

```sql
-- canonical printing cache; one row per Scryfall printing
CREATE TABLE scryfall_cards (
    scryfall_id           TEXT PRIMARY KEY,           -- uuid as text
    oracle_id             TEXT,                       -- shared across reprints
    name                  TEXT NOT NULL,
    set_code              TEXT NOT NULL,              -- lowercased
    set_name              TEXT NOT NULL,
    collector_number      TEXT NOT NULL,
    rarity                TEXT NOT NULL,
    lang                  TEXT NOT NULL,              -- 'en','ja',...
    type_line             TEXT,
    oracle_text           TEXT,
    mana_cost             TEXT,
    cmc                   REAL,
    colors                TEXT,                       -- JSON array, e.g. ["U","R"]
    color_identity        TEXT,                       -- JSON array
    finishes              TEXT,                       -- JSON array, ["nonfoil","foil","etched"]
    image_normal_uri      TEXT,
    image_small_uri       TEXT,
    image_art_crop_uri    TEXT,
    card_faces_json       TEXT,                       -- raw card_faces array for DFCs
    rulings_uri           TEXT,
    tcgplayer_id          INTEGER,                    -- nullable
    tcgplayer_etched_id   INTEGER,                    -- nullable
    legalities_json       TEXT,
    raw_json              TEXT NOT NULL,              -- full Scryfall blob; cheap insurance
    fetched_at            TIMESTAMP NOT NULL
);
CREATE INDEX ix_scryfall_set_num ON scryfall_cards(set_code, collector_number);
CREATE INDEX ix_scryfall_oracle  ON scryfall_cards(oracle_id);
CREATE INDEX ix_scryfall_tcg     ON scryfall_cards(tcgplayer_id);
CREATE INDEX ix_scryfall_name    ON scryfall_cards(name COLLATE NOCASE);

-- one row per (printing × foil × condition × language); the upsert key
CREATE TABLE collection_entries (
    id                INTEGER PRIMARY KEY,
    scryfall_id       TEXT NOT NULL REFERENCES scryfall_cards(scryfall_id),
    finish            TEXT NOT NULL,                  -- 'nonfoil'|'foil'|'etched'
    condition         TEXT NOT NULL,                  -- NM|LP|MP|HP|DMG
    language          TEXT NOT NULL,                  -- mirrors Scryfall lang codes
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    purchase_price    REAL,                           -- per-copy
    purchase_currency TEXT,
    purchase_date     DATE,
    notes             TEXT,
    for_trade         INTEGER NOT NULL DEFAULT 0,
    altered           INTEGER NOT NULL DEFAULT 0,
    misprint          INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL,
    UNIQUE(scryfall_id, finish, condition, language)
);
CREATE INDEX ix_entries_for_trade ON collection_entries(for_trade) WHERE for_trade = 1;

-- many-to-many user tags (kept separate so bulk tag ops don't rewrite entries)
CREATE TABLE tags (
    id   INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE entry_tags (
    entry_id INTEGER NOT NULL REFERENCES collection_entries(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, tag_id)
);

-- daily price snapshot per (tcgplayer_id, subtype)
CREATE TABLE price_history (
    tcgplayer_id     INTEGER NOT NULL,
    sub_type         TEXT NOT NULL,                  -- 'Normal'|'Foil'|'Foil Etched'
    as_of            DATE NOT NULL,
    low_price        REAL,
    mid_price        REAL,
    high_price       REAL,
    market_price     REAL,
    direct_low_price REAL,
    PRIMARY KEY (tcgplayer_id, sub_type, as_of)
);
CREATE INDEX ix_price_recent ON price_history(tcgplayer_id, sub_type, as_of DESC);

-- import job audit
CREATE TABLE import_runs (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,                   -- 'manabox'
    filename        TEXT,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    rows_total      INTEGER,
    rows_imported   INTEGER,
    rows_skipped    INTEGER,
    rows_unmatched  INTEGER,
    error           TEXT
);

-- key/value settings
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

**Price stats: compute on read.** A 90-day window over ~5000 rows is a sub-millisecond
SQLite scan with `ix_price_recent`. A materialized `price_stats` table would have to be
invalidated on every refresh and on backfills, and the payoff is shaving submilliseconds
off page renders. Use a SQL view or a small Python helper that does
`SELECT … FROM price_history WHERE tcgplayer_id=? AND as_of >= date('now','-30 day')`.
Cache aggregated values *per request* in the service if a page renders 50 sparklines.

**`tcgplayer_id` vs `tcgplayer_etched_id`:** Scryfall provides both. The service picks
`tcgplayer_etched_id` when `finish='etched'`, else `tcgplayer_id`. The `sub_type` filter
on `price_history` (`Normal` / `Foil`) disambiguates foil vs nonfoil for the same product.

## 4. Key data flows

### 4a. Manabox CSV import (POST `/import/preview` → `/import/commit`)

```
Browser ──multipart upload──► routers/imports.py:preview
                                  │
                                  ▼
                         services/import_manabox.py
                                  │
                                  │ 1. clients/manabox_csv.py parses with csv.DictReader,
                                  │    normalizes headers (lowercase, strip), maps to ImportRow dataclass.
                                  │
                                  │ 2. For each row, resolve scryfall_id:
                                  │      (a) Scryfall ID present and in scryfall_cards → use it (no API call)
                                  │      (b) (set_code, collector_number) in scryfall_cards → use it
                                  │      (c) else queue for live Scryfall fetch
                                  │    Batch (c) under 75 rows into POST /cards/collection (1 request).
                                  │
                                  │ 3. Compute upsert verdict:
                                  │      key = (scryfall_id, finish, condition, language)
                                  │      if exists: action='increment', new_qty=existing+row.qty
                                  │      else:      action='insert'
                                  │    Unresolvable rows → action='unmatched'.
                                  │
                                  ▼
                          Render templates/import_preview.html with:
                          - verdict counts, summary table, unmatched list with reason,
                          - hidden form holding a server-side session key.
                                  │
   user clicks Commit ──POST──►  routers/imports.py:commit
                                  │
                                  ▼
                         services/import_manabox.py:commit
                                  - one transaction, executemany inserts/updates,
                                  - writes import_runs row with totals,
                                  - redirect to /collection?import_run=NN with flash.
```

**Invariant:** preview does *all* Scryfall I/O. Commit is pure SQL, never touches
the network. If commit did API calls, a slow Scryfall response would stall the request
and a partial failure would leave the DB inconsistent.

### 4b. Price refresh (POST `/prices/refresh` → HTMX-polled progress)

```
User clicks "Refresh Prices" ──POST─► routers/prices.py:refresh
                                          │
                                          │ 1. groups = SELECT DISTINCT set_code FROM scryfall_cards
                                          │             WHERE scryfall_id IN (SELECT scryfall_id FROM collection_entries)
                                          │    Map set_code → tcgplayer groupId via cached groups index
                                          │    (with overrides JSON file for divergent abbreviations).
                                          │
                                          │ 2. job_id = registry.create(total=len(groups))
                                          │    BackgroundTasks.add_task(run_refresh, job_id, groups)
                                          │
                                          │ 3. Return HTML fragment: progress bar with
                                          │    hx-get=/jobs/{id} hx-trigger="every 1s" hx-swap="outerHTML"
                                          │
                                          ▼
                          jobs/runner.py:run_refresh (in thread)
                                  for group in groups:
                                      json = clients/tcgcsv.get_prices(group)      # 100ms sleep between
                                      bulk insert price_history rows for today's date
                                      registry.update(job_id, done=i)
                                  registry.complete(job_id)
                                          │
                          GET /jobs/{id} ─┘  returns progress bar or "done" partial
```

JSON `/prices` over `ProductsAndPrices.csv`: ~90KB per set vs ~400KB, and we already have
product metadata via Scryfall. The CSV is only right for a cold-start full sync, which
we don't need — collection-scoped refresh is sufficient.

TCGCSV updates ~20:00 UTC daily; same-day refresh before that returns yesterday's data.
Show the `last-updated.txt` timestamp on the prices page so this isn't confusing.

### 4c. Search (GET `/collection?q=…&set=…&color=U&page=2`)

```
HTMX request from filter panel (hx-get="/collection/table" hx-include="form")
                                          │
                                          ▼
                          routers/collection.py:table
                          - Pydantic FilterSpec parses query params
                          - calls services/collection.py:search(filter_spec, page, sort)
                                          │
                                          ▼
                          services/collection.py
                          - SQLAlchemy select() joining collection_entries → scryfall_cards
                            LEFT JOIN newest-as_of-per-(tcgplayer_id+subtype) of price_history
                          - filters: ILIKE name, set_code IN, JSON-contains colors, type_line ILIKE,
                            cmc range, rarity IN, price range, qty range, finish/condition/language eq,
                            for_trade eq, tag IN
                          - sort: whitelist of allowed columns; default name ASC
                          - count() + .limit().offset() for pagination
                                          │
                                          ▼
                          Renders partials/card_table.html (HTMX swaps tbody; filter form stays)
```

Every filter input: `hx-trigger="input changed delay:200ms, search"`, `hx-target="#card-table"`.
The whole search interaction is one route returning a fragment. `hx-push-url=true`
preserves refresh and back-button.

## 5. External API integration notes

### Scryfall (`api.scryfall.com`)

Docs: `/docs/api`, `/docs/api/cards`, `/docs/api/bulk-data`, `/docs/api/rate-limits`.

- **Required headers:** `User-Agent: card_catalog/0.1 (purpose)` set explicitly, and
  `Accept: application/json;q=0.9,*/*;q=0.8`.
- **Rate limits (hard):** 500ms between `/cards/search`, `/cards/named`, `/cards/random`,
  `/cards/collection`; 100ms between others. 429 → 30-second lockout; repeated abuse → ban.
  Implement **per-endpoint token buckets** in `clients/scryfall.py` — sharing one bucket
  across endpoint classes triggers 429s under the stricter search bucket.
- **Endpoints used:**
  - `GET /cards/{code}/{number}` — by set + collector number. Cleanest identifier for a Manabox row.
  - `POST /cards/collection` — batch up to 75 identifiers per request. Cuts 75 round trips to one.
  - `GET /cards/tcgplayer/{id}` — reverse lookup from a price row, rarely needed.
  - `GET /bulk-data` — lists bulk files. Use `default_cards` (~514MB, English-or-printed)
    for cold start. Stream via `ijson` to keep memory bounded. Do **not** use `all_cards`
    (2.34GB) — multilingual reprints aren't needed.
- **Images:** Scryfall's CDN at `*.scryfall.io` is unlimited. Store URLs only; the optional
  local cache fetches PNGs on demand to `data/images/{scryfall_id}.jpg`.
- **Gotchas:** DFCs/MDFCs/split cards have `image_uris` on each face, not on the parent.
  Templates must coalesce. Some cards lack `tcgplayer_id` entirely (custom promos) —
  surface this on the detail page.

### TCGCSV (`tcgcsv.com`)

Docs: `/docs`, `/faq`, plus live probing.

- **MTG categoryId = 1** (confirmed via `GET /tcgplayer/categories`).
- **Endpoints (all live):**
  - `GET /tcgplayer/1/groups` — JSON, all MTG groups (sets) with `groupId`, `name`,
    `abbreviation`, `publishedOn`. ~89KB.
  - `GET /tcgplayer/1/{groupId}/products` — JSON product list.
  - `GET /tcgplayer/1/{groupId}/prices` — JSON rows of
    `{productId, lowPrice, midPrice, highPrice, marketPrice, directLowPrice, subTypeName}`.
    `subTypeName` ∈ `{"Normal","Foil","Foil Etched"}`. **This is what we use for refresh.**
  - `GET /tcgplayer/1/{groupId}/ProductsAndPrices.csv` — combined; only useful for cold start.
  - `GET /last-updated.txt` — ISO8601 of last refresh (~20:00 UTC daily).
  - `GET /archive/tcgplayer/prices-YYYY-MM-DD.ppmd.7z` — historical daily archives back to
    **2024-02-08**. 7z-compressed (`ppmd` codec). Requires `py7zr`. Deferred.
- **Rate limit / etiquette:** Custom `User-Agent` required. `time.sleep(0.1)` between
  requests. >10,000 requests/24h risks ban. Our scope (~50 groups) = ~5s of polite sleeps.
- **Gotchas:** `marketPrice` is `null` for many low-volume printings — fall back to
  `midPrice`. Some `productId`s have no matching `tcgplayer_id` in `scryfall_cards`
  (sealed product, accessories) — ignored by the join.

### Manabox CSV

Sources:

- Manabox docs: `/guides/collection/import-export/` (lists *importable* fields only).
- `sboulema/ManaBoxImporter` (C#, CsvHelper-based) — writes Manabox-compatible CSVs
  from MTGA inventory sources. Its `InventoryCard.cs` confirms exact header spellings
  via `[Name(...)]` attributes on properties. **Note:** this importer only emits the
  fields derivable from MTGA exports, so condition / foil / language / purchase columns
  aren't witnessed by this source.

Comma delimiter, header row present, `CultureInfo.InvariantCulture` (so prices use `.`
as decimal separator, not locale-dependent).

| Canonical          | Confirmed / likely header             | Source                                  |
|--------------------|---------------------------------------|-----------------------------------------|
| `name`             | `Name` ✅                              | InventoryCard.cs (default name)         |
| `set_code`         | `Set code` ✅                          | `[Name("Set code")]`                    |
| `set_name`         | `Set name` ✅                          | `[Name("Set name")]`                    |
| `collector_number` | `Collector number` ✅                  | `[Name("Collector number")]`            |
| `scryfall_id`      | `Scryfall ID` ✅ (Guid format)         | `[Name("Scryfall ID")]`                 |
| `quantity`         | `Quantity` ✅ (int)                    | InventoryCard.cs (default name)         |
| `foil`             | `Foil` — `normal` / `foil` / `etched` | Manabox docs (unconfirmed spelling)     |
| `condition`        | `Condition` — `NM`/`LP`/…             | Manabox docs (unconfirmed spelling)     |
| `language`         | `Language` — `English`/`Japanese`/…   | Manabox docs (unconfirmed spelling)     |
| `purchase_price`   | `Purchase price`                      | Manabox docs (unconfirmed spelling)     |
| `purchase_currency`| `Purchase currency`                   | Manabox docs (unconfirmed spelling)     |
| `misprint`         | `Misprint`                            | Manabox docs (unconfirmed spelling)     |
| `altered`          | `Altered`                             | Manabox docs (unconfirmed spelling)     |

Parser policy: read the header row and case-insensitively map to canonical names, with
the confirmed spellings as the primary match and lowercase/spacing variants as fallbacks.
Validate; log unknown columns (warn, don't fail). Reject rows with no `scryfall_id`
AND no `(set_code, collector_number)` pair as unmatched up front. Capture the first real
Manabox export as `tests/fixtures/manabox_sample.csv` to lock in the remaining unconfirmed
spellings.

### Archidekt

References reviewed:

- Gist `JasonFreeberg/203c651987b124cb74e36f456a415c1d` — *not* an API client. A Python
  CSV transformation that pulls `Quantity` + `Scryfall ID` out of a Manabox export and
  writes a two-column CSV "that can then be uploaded using Archidekt.com's CSV upload
  utility." Manual web upload, not programmatic.
- `linkian209/pyrchidekt` (Python, 17 stars, updated 2026-04-19) — Archidekt API wrapper.
  README example is read-only: `getDeckById(1)` returns a `Deck` with `categories[*].cards[*]`.
  No documented login/key flow, no documented create/update endpoints.

**Bottom line:** there is no documented, library-supported way to *push* a collection
into Archidekt programmatically. Two realistic features instead:

1. **Archidekt CSV export (push direction, manual final step).** Produce a CSV in the
   format Archidekt's web import accepts. Per the gist, two columns are sufficient:
   `Quantity, Scryfall ID`. User downloads the file and uploads it via Archidekt's
   web UI. Filter-aware: export the *current filtered view* of the collection, not
   always the whole thing (so a user can export "all standard-legal cards I own as
   trade fodder" without manual editing).
2. **Archidekt deck reconciliation (pull direction, fully programmatic).** Using
   `pyrchidekt.api.getDeckById`, fetch a public Archidekt deck by ID or URL and
   render a "what I'm missing" report against the local collection. This is the
   more interesting integration and uses pyrchidekt for what it actually does.

Auth: neither path needs credentials in our scope (export is a file download; deck
reconciliation reads public decks). If Archidekt ever exposes an authenticated upload
endpoint, the gap to fill is small — a settings field for an API token and a POST
helper in `clients/archidekt.py`.

Gotchas:

- Archidekt's CSV importer historically accepts more columns (name, set code, foil,
  category, etc.) but the minimum that round-trips reliably is `Quantity, Scryfall ID`.
  Ship the minimum first; widen if a real export reveals the user wants categories
  preserved.
- `pyrchidekt` is small and lightly maintained. Pin a known-good version in
  `pyproject.toml` and wrap it behind `clients/archidekt.py` so a future swap
  (or upstream breakage) is a one-file change.

### TCGplayer official API

Docs: `/docs/getting-started`.

- **Access is gated and no longer granted to new developers** (per the linked doc).
- OAuth2 client_credentials → `POST /token` with `PUBLIC_KEY`/`PRIVATE_KEY`, bearer
  expires in ~14 days.
- **What it offers beyond TCGCSV:** real-time prices, seller listings, buylist data,
  direct purchase URLs, SKU-level granularity. **None required for the brief.**

**Recommendation: do not integrate.** TCGCSV covers 100% of the brief's price needs;
new API access isn't being granted; direct buy links don't need an API
(`https://www.tcgplayer.com/product/{tcgplayer_id}`). The settings page keeps the key
field for forward-compat, marked "(unused — TCGCSV provides prices)".

## 6. Implementation order

Each milestone is independently runnable and demoable.

1. **M1 — Repo skeleton & DB foundation.** `uv init`, FastAPI hello at `127.0.0.1:8000`,
   Jinja2 + Tailwind compiled output rendering, SQLAlchemy engine with WAL pragmas,
   Alembic init, first migration creates all base tables, seed `settings` defaults.
   Settings page reads/writes them.
   **Done when:** `uv run uvicorn card_catalog.main:app --reload` boots and the settings
   form persists a value across restarts.

2. **M2 — Scryfall client + cold-start cache.** `clients/scryfall.py` with token-bucket
   rate limiter and required headers. `services/enrich_scryfall.py:bulk_sync()` streams
   `default_cards` via `ijson`, inserts into `scryfall_cards`. Manual trigger from
   settings page.
   **Done when:** ~110k Scryfall rows are present and a card detail page renders for
   a hardcoded `scryfall_id`.

3. **M3 — Manabox import (cache-only resolver).** `clients/manabox_csv.py` header-tolerant
   parser, `services/import_manabox.py:preview` resolving only against local
   `scryfall_cards`. Routes for `/import`, `/import/preview`, `/import/commit`. Preview
   template with verdict counts and unmatched list.
   **Done when:** a real Manabox export imports cleanly using the M2 cache, with a clean
   diff in `collection_entries`.

4. **M4 — Live Scryfall fallback in import.** Cache misses get batched into
   `POST /cards/collection` (75/batch); residuals go to single `GET /cards/{code}/{number}`.
   Newly fetched cards inserted into `scryfall_cards` on the fly.
   **Done when:** importing a CSV with a brand-new set still produces zero unmatched rows.

5. **M5 — Collection browse, filter, sort, paginate.** `services/collection.py:search()`,
   `routers/collection.py`. Filter panel (HTMX), sortable columns (`hx-push-url`),
   pagination. Card detail page with Scryfall image, oracle text, rulings (fetched lazily
   and stored in a `card_rulings` cache table — added in M5's migration).
   **Done when:** a 5k-card collection paginates at 50 rows/page in under 100ms per swap.

6. **M6 — Edit & bulk edit.** Inline edit-in-place for quantity/condition/foil/notes/tags/
   for-trade via HTMX `hx-patch` returning the updated row partial. Bulk edit: row
   checkboxes, sticky action bar, one POST applies partial update to all selected.
   **Done when:** all editable columns work individually and in bulk.

7. **M7 — Price refresh.** TCGCSV groups index sync, `services/prices.py:refresh()`,
   background job + progress endpoint, sparkline on detail page (SQL agg → SVG path
   in Jinja). Show "last refreshed" and TCGCSV `last-updated.txt`.
   **Done when:** clicking refresh populates `price_history` for today, sparklines render
   for cards with ≥2 days of history, 30d avg/% change appear in the collection list.

8. **M8 — Archidekt CSV export.** A toolbar button on the collection page exports the
   *current filtered view* as `Quantity, Scryfall ID` CSV (the minimum format Archidekt's
   web importer accepts). Implemented as `GET /archidekt/export.csv?{same filter params
   as collection search}` — reuses `services/collection.py:search()`, streams CSV rows
   via `StreamingResponse`. Rows whose `scryfall_id` is missing (shouldn't happen post-M4
   but guard anyway) are skipped and counted in a `X-Skipped-Rows` response header.
   User downloads, then uploads via archidekt.com manually.
   **Done when:** a filter like "color=R, rarity=mythic, qty>=1" produces a CSV that
   imports cleanly into a fresh Archidekt deck.

9. **M9 — Archidekt deck reconciliation.** Add `pyrchidekt` to deps, wrap in
   `clients/archidekt.py` so an upstream break is a one-file change. New page
   `/archidekt/reconcile`: paste an Archidekt deck URL or ID → fetch with `getDeckById`
   → render a table showing each `(card, needed_qty, owned_qty, missing_qty)`. "Owned"
   matches by `oracle_id` (deck slots don't usually pin a printing). Link missing rows
   to the Scryfall search and a TCGplayer buy URL.
   **Done when:** pasting a real public commander deck shows correct missing/owned counts
   against the local collection.

10. **M10 — Polish.** Optional image cache (lazy download to `data/images/`), Manabox
    CSV round-trip export, settings UI for currency display, default condition,
    Scryfall delay. Empty-states and error toasts via HTMX out-of-band swaps.

**Deferred / not in this plan:**

- TCGCSV historical backfill from `archive/*.7z` (adds `py7zr` and streaming-7z complexity).
- TCGplayer official API (gated, redundant with TCGCSV).
- Programmatic *push* to Archidekt — no documented API path; revisit if Archidekt
  publishes an authenticated upload endpoint.
- Deck builder / multi-collection / cloud sync.
- Background scheduler.

## 7. Open questions and risks

1. **Collection scale.** Hundreds, low thousands, or 10k+? Affects bulk-edit UX
   (10k → server-side select-all-matching-filter is mandatory; 500 → checkboxes are fine),
   pagination strategy (10k → keyset pagination beats offset), and Scryfall cold-import
   time (10k import ≈ 10–15 min over the rate limiter even with `/cards/collection`).
   **Needed before M3.**
2. **Real Manabox export headers — narrowed.** Core columns are confirmed via
   `sboulema/ManaBoxImporter`'s CsvHelper attributes (`Set code`, `Set name`,
   `Collector number`, `Scryfall ID`, `Name`, `Quantity`). Still unconfirmed:
   exact spellings of `Foil`, `Condition`, `Language`, `Purchase price`,
   `Purchase currency`, `Misprint`, `Altered`, plus their value vocabularies
   (e.g. `NM` vs `Near Mint`, `normal` vs `nonfoil`). A 5-row export from your
   real collection nails this down — useful before M3 but not blocking, since
   the parser is header-tolerant.
3. **TCGplayer API key.** Does the user have one? If not, the settings field stays
   disabled. If yes, direct buy links / seller-count badges land in M8.
4. **Set-code → TCGplayer group mapping.** Most match by abbreviation case-insensitively,
   but ~10–20 diverge (Universes Beyond, Secret Lair drops, Commander variants).
   Plan ships a `src/card_catalog/data/tcgcsv_group_overrides.json` populated reactively
   when the refresh encounters unmatched sets. Most likely place real-world friction
   appears.
5. **Foreign-language rows.** A Japanese Bloodghast and an English Bloodghast are
   different `scryfall_id`s; the upsert key handles them. But Manabox sometimes exports
   only the English `Name` + a non-`en` `Language` cell, with `Scryfall ID` pointing at
   the English printing. Recommend: trust `Scryfall ID` if present; else resolve
   `(set, number, lang)` via `/cards/{code}/{number}/{lang}`. Confirm before M4.
6. **Currency.** Manabox can export `Purchase currency` per row, but Scryfall/TCGCSV
   prices are USD only. Initial decision: store USD natively, display USD only, defer FX.
7. **Job concurrency.** Two browser tabs hitting "Refresh Prices" simultaneously would
   double-work. `JobRegistry` enforces a per-job-type singleton: if a refresh job is
   already running, return its id instead of starting a new one. Cheap; called out
   so it isn't missed in M7.
