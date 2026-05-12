"""Price refresh orchestration.

Flow (collection-scoped):
  1. Find every set_code present in the user's collection.
  2. Map set_code → TCGplayer groupId via TCGCSV's groups index, with a
     small hand-curated overrides file as a tie-breaker.
  3. For each mapped group, fetch /prices and upsert today's rows into
     `price_history` (PK = tcgplayer_id, sub_type, as_of).
  4. Unmapped sets surface in `job.extras['unmapped_sets']` so the UI
     can prompt the user to add an override.

Price stats are deliberately computed-on-read. See PLAN.md §3.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from card_catalog.clients.tcgcsv import TCGCSV, TCGCSVError
from card_catalog.db.models import CollectionEntry, PriceHistory, ScryfallCard
from card_catalog.jobs.registry import JobState, registry
from card_catalog.utils import utc_now

log = logging.getLogger(__name__)


# ---------- groups cache & overrides -----------------------------------------

_OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "data" / "tcgcsv_group_overrides.json"

_groups_cache_lock = threading.Lock()
_groups_cache: list[dict] | None = None


def _load_overrides() -> dict[str, int]:
    """Read tcgcsv_group_overrides.json. Returns {set_code_lower: groupId}.

    Missing or malformed file → empty map (overrides are optional).
    """
    try:
        raw = _OVERRIDES_PATH.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log.warning("tcgcsv_group_overrides.json is not valid JSON; ignoring")
        return {}
    if not isinstance(data, dict):
        log.warning(
            "tcgcsv_group_overrides.json must be a JSON object; got %s",
            type(data).__name__,
        )
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        try:
            out[str(k).strip().lower()] = int(v)
        except (TypeError, ValueError):
            log.warning("ignoring override %r: not an int", {k: v})
    return out


def _refresh_groups_cache(client: TCGCSV) -> list[dict]:
    """Repopulate the module-global groups cache from TCGCSV."""
    global _groups_cache
    groups = client.list_groups()
    with _groups_cache_lock:
        _groups_cache = groups
    return groups


def _build_set_code_index(groups: list[dict]) -> dict[str, int]:
    """abbreviation (lowercased) → groupId. Falls back to nothing on collisions.

    TCGCSV abbreviations are unique within the MTG category in practice; if a
    duplicate shows up we keep the *latest* by publishedOn, which biases toward
    the canonical set rather than a promo bundle that reuses an old code.
    """
    out: dict[str, tuple[int, str]] = {}  # code -> (groupId, publishedOn)
    for g in groups:
        abbrev = (g.get("abbreviation") or "").strip().lower()
        gid = g.get("groupId")
        if not abbrev or gid is None:
            continue
        published = str(g.get("publishedOn") or "")
        prior = out.get(abbrev)
        if prior is None or published > prior[1]:
            out[abbrev] = (int(gid), published)
    return {code: gid for code, (gid, _) in out.items()}


# ---------- the refresh job --------------------------------------------------


def refresh_collection_prices(
    db: Session,
    job: JobState,
    tcgcsv_client: TCGCSV,
) -> None:
    """Collection-scoped daily price refresh. See module docstring."""

    # 1. Distinct set codes covered by the user's collection.
    set_codes: list[str] = list(
        db.scalars(
            select(ScryfallCard.set_code)
            .distinct()
            .where(
                ScryfallCard.scryfall_id.in_(
                    select(CollectionEntry.scryfall_id).distinct()
                )
            )
        ).all()
    )
    set_codes = [(c or "").strip().lower() for c in set_codes if c]

    if not set_codes:
        registry.complete(job.id, detail="No collection entries to price.")
        job.extras["updates"] = 0
        job.extras["unmapped_sets"] = []
        job.extras["mapped_sets"] = 0
        return

    # 2. Build set_code → groupId map. Cache groups list at refresh start.
    registry.update(job.id, detail="Fetching TCGCSV group index…")
    groups = _refresh_groups_cache(tcgcsv_client)
    code_to_group = _build_set_code_index(groups)
    overrides = _load_overrides()
    code_to_group.update(overrides)  # overrides win
    group_meta = {int(g["groupId"]): g for g in groups if g.get("groupId") is not None}

    mapped: list[tuple[str, int, str]] = []  # (set_code, groupId, set_name)
    unmapped: list[str] = []
    for code in set_codes:
        gid = code_to_group.get(code)
        if gid is None:
            unmapped.append(code)
            continue
        name = (group_meta.get(gid, {}).get("name")) or code.upper()
        mapped.append((code, gid, name))

    job.total = len(mapped)
    job.done = 0
    job.extras["unmapped_sets"] = sorted(unmapped)
    job.extras["mapped_sets"] = len(mapped)
    job.extras["updates"] = 0
    job.extras["started_at_iso"] = utc_now().isoformat(timespec="seconds")

    if not mapped:
        registry.complete(
            job.id,
            detail=f"No set codes mapped to TCGplayer groups ({len(unmapped)} unmapped).",
        )
        return

    # 3. For each mapped group, fetch prices and upsert.
    today = date.today()
    total_rows = 0
    for i, (code, gid, name) in enumerate(mapped, start=1):
        registry.update(job.id, done=i - 1, detail=f"Refreshing {name}…")
        try:
            rows = tcgcsv_client.get_prices(gid)
        except TCGCSVError as exc:
            log.warning("group %s (%s) failed: %s", code, gid, exc)
            registry.update(job.id, done=i, detail=f"Skipped {name}: {exc}")
            continue

        inserted = _upsert_prices(db, rows, today)
        total_rows += inserted
        job.extras["updates"] = total_rows
        registry.update(job.id, done=i, detail=f"Refreshed {name} ({inserted} rows)")

    job.extras["finished_at_iso"] = utc_now().isoformat(timespec="seconds")
    registry.complete(
        job.id,
        detail=(
            f"Updated {total_rows:,} price rows across {len(mapped)} sets"
            + (f" · {len(unmapped)} unmapped" if unmapped else "")
        ),
    )


def _upsert_prices(db: Session, rows: list[dict], as_of: date) -> int:
    """Upsert one set's price rows into price_history for `as_of`.

    Returns the count of rows we actually fed to the upsert (some rows may
    be filtered if their productId is missing).
    """
    payload: list[dict[str, Any]] = []
    for r in rows:
        product_id = r.get("productId")
        sub_type = r.get("subTypeName")
        if product_id is None or not sub_type:
            continue
        payload.append(
            {
                "tcgplayer_id": int(product_id),
                "sub_type": str(sub_type),
                "as_of": as_of,
                "low_price": _to_float(r.get("lowPrice")),
                "mid_price": _to_float(r.get("midPrice")),
                "high_price": _to_float(r.get("highPrice")),
                "market_price": _to_float(r.get("marketPrice")),
                "direct_low_price": _to_float(r.get("directLowPrice")),
            }
        )
    if not payload:
        return 0

    # Chunk to keep parameter counts under SQLite's 999/32766 ceiling.
    chunk = 400
    written = 0
    for i in range(0, len(payload), chunk):
        sub = payload[i : i + chunk]
        sub_stmt = sqlite_insert(PriceHistory).values(sub)
        sub_stmt = sub_stmt.on_conflict_do_update(
            index_elements=["tcgplayer_id", "sub_type", "as_of"],
            set_={
                "low_price": sub_stmt.excluded.low_price,
                "mid_price": sub_stmt.excluded.mid_price,
                "high_price": sub_stmt.excluded.high_price,
                "market_price": sub_stmt.excluded.market_price,
                "direct_low_price": sub_stmt.excluded.direct_low_price,
            },
        )
        db.execute(sub_stmt)
        written += len(sub)
    db.commit()
    return written


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # TCGCSV uses 0.0 as a soft "missing" sentinel on many fields. Keep it as
    # 0.0 — coalesce on read decides whether to fall back.
    return f


# ---------- read-side helpers ------------------------------------------------


def _resolve_product_ids(entry: CollectionEntry) -> tuple[int | None, str]:
    """Return (tcgplayer_id, sub_type) for a collection entry."""
    card = entry.card
    finish = (entry.finish or "nonfoil").lower()
    if finish == "etched":
        pid = card.tcgplayer_etched_id or card.tcgplayer_id
        sub_type = "Foil Etched"
        # Some sets list etched foils under "Foil" too; the caller falls back.
        return (pid, sub_type)
    if finish == "foil":
        return (card.tcgplayer_id, "Foil")
    return (card.tcgplayer_id, "Normal")


def latest_price_for_entry(db: Session, entry: CollectionEntry) -> float | None:
    """Most recent market_price (or mid_price) for an entry. None if no history."""
    pid, sub_type = _resolve_product_ids(entry)
    if pid is None:
        return None

    sub_types = [sub_type]
    # "Foil Etched" sometimes lands in TCGCSV as plain "Foil" — try both.
    if sub_type == "Foil Etched":
        sub_types.append("Foil")

    row = db.execute(
        select(PriceHistory.market_price, PriceHistory.mid_price)
        .where(
            PriceHistory.tcgplayer_id == pid,
            PriceHistory.sub_type.in_(sub_types),
        )
        .order_by(PriceHistory.as_of.desc())
        .limit(1)
    ).first()
    if not row:
        return None
    market, mid = row
    if market and market > 0:
        return float(market)
    if mid and mid > 0:
        return float(mid)
    return None


def history_points(
    db: Session,
    tcgplayer_id: int,
    sub_type: str = "Normal",
    days: int = 30,
) -> list[float]:
    """Oldest→newest price points suitable for the `sparkline` macro.

    Prefers `market_price` and falls back to `mid_price` on a per-day basis
    so a missing market price doesn't punch a hole in the line.
    """
    if not tcgplayer_id:
        return []
    from datetime import timedelta

    cutoff = date.today() - timedelta(days=int(days))
    rows = db.execute(
        select(PriceHistory.as_of, PriceHistory.market_price, PriceHistory.mid_price)
        .where(
            PriceHistory.tcgplayer_id == tcgplayer_id,
            PriceHistory.sub_type == sub_type,
            PriceHistory.as_of >= cutoff,
        )
        .order_by(PriceHistory.as_of.asc())
    ).all()
    out: list[float] = []
    for _, market, mid in rows:
        v = market if (market and market > 0) else mid
        if v and v > 0:
            out.append(float(v))
    return out


def _ensure_latest_price_view(db: Session) -> None:
    """Create the v_entry_latest_price helper view if it doesn't exist.

    The view joins collection_entries → scryfall_cards → most-recent
    price_history row for the appropriate (tcgplayer_id, sub_type).
    Used by `collection_value_estimate` to keep the query short.
    """
    db.execute(
        text(
            """
            CREATE VIEW IF NOT EXISTS v_entry_latest_price AS
            SELECT
                ce.id            AS entry_id,
                ce.quantity      AS quantity,
                ce.scryfall_id   AS scryfall_id,
                ce.finish        AS finish,
                COALESCE(
                    CASE WHEN ce.finish = 'etched'
                         THEN sc.tcgplayer_etched_id
                         ELSE sc.tcgplayer_id END,
                    sc.tcgplayer_id
                )                AS pid,
                CASE
                    WHEN ce.finish = 'etched' THEN 'Foil Etched'
                    WHEN ce.finish = 'foil'   THEN 'Foil'
                    ELSE                            'Normal'
                END              AS sub_type
            FROM collection_entries ce
            JOIN scryfall_cards sc ON sc.scryfall_id = ce.scryfall_id;
            """
        )
    )
    db.commit()


def collection_value_estimate(db: Session) -> float:
    """Sum of qty * coalesce(market, mid) across the collection. USD."""
    _ensure_latest_price_view(db)
    row = db.execute(
        text(
            """
            WITH latest AS (
                SELECT
                    v.entry_id,
                    v.quantity,
                    (
                        SELECT COALESCE(ph.market_price, ph.mid_price, 0.0)
                        FROM price_history ph
                        WHERE ph.tcgplayer_id = v.pid
                          AND ph.sub_type     = v.sub_type
                        ORDER BY ph.as_of DESC
                        LIMIT 1
                    ) AS unit_price
                FROM v_entry_latest_price v
                WHERE v.pid IS NOT NULL
            )
            SELECT COALESCE(SUM(quantity * unit_price), 0.0) FROM latest;
            """
        )
    ).first()
    return float(row[0] if row and row[0] is not None else 0.0)


# ---------- misc utility used by the router ---------------------------------


def tracked_tcgplayer_count(db: Session) -> int:
    """Distinct cards in the collection that have a TCGplayer product id."""
    return int(
        db.scalar(
            select(func.count(func.distinct(ScryfallCard.tcgplayer_id)))
            .select_from(CollectionEntry)
            .join(ScryfallCard, ScryfallCard.scryfall_id == CollectionEntry.scryfall_id)
            .where(ScryfallCard.tcgplayer_id.is_not(None))
        )
        or 0
    )


def latest_refresh_at(db: Session) -> datetime | None:
    """Most recent `as_of` across `price_history`, returned as a UTC datetime."""
    d: date | None = db.scalar(select(func.max(PriceHistory.as_of)))
    if d is None:
        return None
    return datetime(d.year, d.month, d.day)


__all__ = [
    "TCGCSV",
    "refresh_collection_prices",
    "latest_price_for_entry",
    "history_points",
    "collection_value_estimate",
    "tracked_tcgplayer_count",
    "latest_refresh_at",
]
