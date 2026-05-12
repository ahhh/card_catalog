# Card Catalog

A premium personal MTG collection command center. Local-only. FastAPI + HTMX +
SQLite. Designed to feel like a portable, self-contained vault you'd actually
want to browse, not a tool you'd reluctantly maintain.

```
overview      a glance at the vault: total cards, value, color mix, rarity mix
collection    browse, filter, sort, edit (single or bulk) — grid and list views
import        drag-and-drop a Manabox CSV; preview before committing
prices        refresh from TCGCSV daily; price history sparklines on every card
archidekt     export the collection as Archidekt CSV; reconcile against any
              public Archidekt deck to see what you're missing
settings      profile, defaults, API keys, polite-delay knobs
```

## Quick start

```bash
# Install uv if you don't have it
brew install uv

# Sync runtime + dev deps
uv sync --extra dev

# Create the database (idempotent)
uv run alembic upgrade head

# Run the app
uv run uvicorn card_catalog.main:app --host 127.0.0.1 --port 8765 --reload
```

Open <http://127.0.0.1:8765>.

First-time flow:

1. Open **Import**, click *Sync Scryfall cache* to seed ~110k printings (one-time,
   ~5 minutes, streamed via the `default_cards` bulk file).
2. Drop your Manabox CSV — the preview shows verdict counts (new / increment /
   unmatched) before any DB write. Commit only when the numbers look right.
3. Open **Prices** and click *Refresh* to pull today's TCGCSV snapshot for every
   set in your collection.
4. Browse, edit, share.

## The database is portable

Everything lives in `data/catalog.db`. Copy it to another machine running this
app and your collection follows you. Send it to a friend and they can browse
without an internet connection — card images are URLs on Scryfall's CDN, so
they're cached aggressively by the browser; if a recipient is offline, the
layout still degrades gracefully (placeholder tiles, no broken icons). The
file is under 50 MB for a typical 5k-card collection.

What's intentionally **not** stored:

- Card images (URLs only — Scryfall's CDN is unlimited)
- Historical Scryfall bulk-data dumps (only the latest values are needed)
- Anything derivable from the Scryfall cache + collection rows

What **is** stored: the Scryfall cache (so search/filter works offline), every
collection entry with finish/condition/language fidelity, daily TCGCSV price
snapshots (~2 KB per card-day), import audit trail, your settings, and
tags/notes you've added.

## What's inside

```
src/card_catalog/
  clients/      Scryfall, TCGCSV, Manabox CSV parser, Archidekt
  db/           SQLAlchemy models + session (WAL, FKs on, mmap)
  domain/       Enums, identity helpers (Condition, Finish, normalize_lang)
  jobs/         In-process JobRegistry with singleton guard
  routers/      Thin FastAPI handlers — one per feature area
  services/     Business logic — owns transactions and orchestration
  data/         tcgcsv_group_overrides.json (the only non-DB durable file)
  config.py     pydantic-settings: DB path, UA, polite delays
  main.py       App factory
  utils.py      utc_now() helper

templates/      Jinja2 + HTMX. Hand-authored, no build step.
  partials/     macros (mana_cost, rarity_pip, sparkline, card_tile, …) and
                fragments swapped by HTMX

static/         Compiled CSS, vendored htmx.min.js URL, small app.js for
                slide-overs / toast / chip-toggle / dropzone behaviors
                (no framework)

alembic/        Schema migrations
tests/          311 pytest tests + fixtures + sample Manabox CSV
```

## Design system

- **Typography:** Fraunces (variable serif) for display, Inter Tight for body,
  JetBrains Mono for numerics. Loaded from Google Fonts; cached by the browser.
- **Aesthetic:** dark "card vault." Deep ink background, warm gold value
  accents, mana-tinted filter chips, gilt corner badges on foils. Inspired by
  high-end auction catalogs × Bloomberg terminals × a luxe binder.
- **Motion:** subtle, 120–240ms eases. Card hover lifts. Smooth swap-in.
  Skeleton shimmer on long loads. Progress bar shine.
- **Layout:** sticky top nav, optional left sidebar for filters, slide-over
  from the right for inline detail views.
- **No SPA, no JS build.** HTMX drives every partial swap; one ~80-line
  `app.js` glues in slide-over open/close, keyboard shortcuts (`/` focuses
  search, `Esc` closes overlays), chip toggles, bulk-action bar, and drag-drop.

## Development

```bash
# Run the full test suite (311 tests, ~3.5s, 86% coverage)
uv run pytest -q

# With coverage report
uv run pytest --cov=card_catalog --cov-report=term-missing

# Lint
uv run flake8 src/ tests/

# Type-check (optional — no mypy config shipped, but the code is annotated)
uv run python -m mypy src/

# Generate a new migration
uv run alembic revision --autogenerate -m "your change"

# Start the dev server with auto-reload
uv run uvicorn card_catalog.main:app --reload
```

Configuration knobs live in `.env` (see `.env.example`). The settings page
holds anything that's meaningful to tweak from the UI; environment variables
are reserved for startup-only values (DB path, host/port, User-Agent).

### Set-code → TCGplayer group mismatches

Most Scryfall set codes match the TCGCSV abbreviation case-insensitively, but
~10–20 sets diverge (Universes Beyond, Secret Lair drops, Commander variants
sometimes splinter into multiple TCGCSV groups). When a price refresh can't
find a group, the set surfaces in the **Prices** page's "Unmapped sets"
callout. To fix: add a mapping to
`src/card_catalog/data/tcgcsv_group_overrides.json`:

```json
{
  "sld": 2742,
  "30a": 2965
}
```

Restart isn't required — the file is read fresh at the start of every refresh.

## External data sources

- **Scryfall** (`api.scryfall.com`) — card metadata, oracle text, images,
  rulings, set info. Per-endpoint token-bucket rate limiter (500ms on
  `/cards/search`/`/cards/named`/`/cards/random`/`/cards/collection`, 100ms
  elsewhere). User-Agent required and configurable in settings. Bulk-data
  cold sync streams the `default_cards` file via `ijson` to keep memory
  bounded.
- **TCGCSV** (`tcgcsv.com`) — daily TCGplayer price snapshots. We use the
  collection-scoped JSON `/prices` endpoint (one request per set in your
  collection). Polite 100ms delay between requests. Shows TCGCSV's
  `last-updated.txt` timestamp so refreshing before 20:00 UTC isn't
  confusing.
- **Manabox CSV** — header-tolerant parser with confirmed spellings
  (`Set code`, `Set name`, `Collector number`, `Scryfall ID`, `Name`,
  `Quantity`) plus alias matching for `Foil`, `Condition`, `Language`,
  `Purchase price`, etc. Unknown columns log a warning and are dropped, not
  an error.
- **Archidekt** — two integrations:
  1. **Export** — produces `Quantity, Scryfall ID` CSV in the format the
     Archidekt web importer accepts. Click *Download*, upload it via
     archidekt.com — one-click round-trip.
  2. **Reconcile** — paste a public deck URL or numeric id; we fetch via
     `pyrchidekt`, match each deck slot to your collection by `oracle_id`,
     and render an owned/missing report with cheapest-printing replacement
     cost. Useful for "what do I need to buy to finish this commander deck?"

The TCGplayer official API is intentionally **not** integrated — new access
isn't being granted, and TCGCSV covers every price need. Direct buy links are
constructed from `tcgplayer_id` without auth.

## Project status

The full plan and architectural rationale is in [`PLAN.md`](./PLAN.md).
Milestones M1–M10 are implemented. Deferred items, all documented in the plan:

- TCGCSV historical backfill from the `archive/*.7z` snapshots (adds
  `py7zr` and streaming-7z complexity).
- Programmatic *push* to Archidekt (no documented API path).
- Multi-user / cloud sync / a deck builder.
- An automatic price-refresh scheduler (manual button is intentional — the
  app should run only when you're using it).

## License

Personal use. MTG, card images, and oracle text remain the property of Wizards
of the Coast.
