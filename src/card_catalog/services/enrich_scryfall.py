"""Scryfall enrichment service.

Owns the `scryfall_cards` table. Two main entry points:
  * `bulk_sync(db, job)` — cold-start: download the `default_cards` bulk-data
    file, stream-parse it, upsert every printing.
  * `lookup_or_fetch(db, set_code, number, lang)` — used by ad-hoc card resolution
    when the cache misses and we don't want to round-trip the whole bulk file.

Everything below treats Scryfall dicts as the source of truth and serializes
JSON arrays as compact strings for storage.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from card_catalog.clients.scryfall import ScryfallClient, get_default_client
from card_catalog.db.models import ScryfallCard
from card_catalog.jobs.registry import JobState, registry
from card_catalog.utils import utc_now

log = logging.getLogger(__name__)

_BATCH_SIZE = 1000


def _json_compact(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def _image_uri(card: dict, key: str) -> str | None:
    uris = card.get("image_uris")
    if isinstance(uris, dict) and uris.get(key):
        return uris[key]
    # DFC / split cards put image_uris under each face.
    faces = card.get("card_faces") or []
    for face in faces:
        f_uris = face.get("image_uris") if isinstance(face, dict) else None
        if isinstance(f_uris, dict) and f_uris.get(key):
            return f_uris[key]
    return None


def _row_from_scryfall(card: dict) -> dict[str, Any]:
    """Map a Scryfall raw dict → a row dict suitable for INSERT/REPLACE."""
    return {
        "scryfall_id": card["id"],
        "oracle_id": card.get("oracle_id"),
        "name": card.get("name", ""),
        "set_code": (card.get("set") or "").lower(),
        "set_name": card.get("set_name", ""),
        "collector_number": str(card.get("collector_number", "")),
        "rarity": card.get("rarity", "common"),
        "lang": card.get("lang", "en"),
        "type_line": card.get("type_line"),
        "oracle_text": card.get("oracle_text"),
        "mana_cost": card.get("mana_cost"),
        "cmc": card.get("cmc"),
        "colors": _json_compact(card.get("colors")),
        "color_identity": _json_compact(card.get("color_identity")),
        "finishes": _json_compact(card.get("finishes")),
        "image_normal_uri": _image_uri(card, "normal"),
        "image_small_uri": _image_uri(card, "small"),
        "image_art_crop_uri": _image_uri(card, "art_crop"),
        "card_faces_json": _json_compact(card.get("card_faces")),
        "rulings_uri": card.get("rulings_uri"),
        "tcgplayer_id": card.get("tcgplayer_id"),
        "tcgplayer_etched_id": card.get("tcgplayer_etched_id"),
        "legalities_json": _json_compact(card.get("legalities")),
        "raw_json": json.dumps(card, separators=(",", ":")),
        "fetched_at": utc_now(),
    }


def _upsert_rows(db: Session, rows: list[dict[str, Any]]) -> None:
    """Bulk INSERT OR REPLACE via SQLite-dialect upsert."""
    if not rows:
        return
    stmt = sqlite_insert(ScryfallCard).values(rows)
    # All updatable cols = everything except the PK.
    update_cols = {
        c.name: getattr(stmt.excluded, c.name)
        for c in ScryfallCard.__table__.columns
        if c.name != "scryfall_id"
    }
    stmt = stmt.on_conflict_do_update(index_elements=[ScryfallCard.scryfall_id], set_=update_cols)
    db.execute(stmt)


def upsert_card_dict(db: Session, card_dict: dict, *, commit: bool = True) -> ScryfallCard | None:
    """Single-row upsert helper. Returns the persisted ORM instance (re-fetched)."""
    if not card_dict or not card_dict.get("id"):
        return None
    row = _row_from_scryfall(card_dict)
    _upsert_rows(db, [row])
    if commit:
        db.commit()
    return db.get(ScryfallCard, row["scryfall_id"])


def upsert_card_dicts(db: Session, cards: list[dict], *, commit: bool = True) -> int:
    """Bulk upsert. Returns number of rows persisted."""
    rows = [_row_from_scryfall(c) for c in cards if c and c.get("id")]
    if not rows:
        return 0
    _upsert_rows(db, rows)
    if commit:
        db.commit()
    return len(rows)


def lookup_or_fetch(
    db: Session,
    set_code: str,
    collector_number: str,
    lang: str = "en",
    *,
    client: ScryfallClient | None = None,
) -> ScryfallCard | None:
    """Return a `ScryfallCard` from the cache, fetching live on miss."""
    code = (set_code or "").strip().lower()
    number = str(collector_number or "").strip()
    if not code or not number:
        return None

    stmt = select(ScryfallCard).where(
        ScryfallCard.set_code == code,
        ScryfallCard.collector_number == number,
    )
    if lang:
        stmt = stmt.where(ScryfallCard.lang == lang)
    existing = db.scalars(stmt).first()
    if existing is not None:
        return existing

    cl = client or get_default_client()
    card_dict = cl.get_by_set_number(code, number, lang=lang)
    if not card_dict:
        return None
    return upsert_card_dict(db, card_dict)


# ---- bulk sync ------------------------------------------------------------


def bulk_sync(
    db: Session,
    job: JobState,
    *,
    bulk_type: str = "default_cards",
    client: ScryfallClient | None = None,
) -> dict[str, Any]:
    """Download + ingest the chosen Scryfall bulk file.

    Updates the job's `done`/`total`/`detail` as it goes. Returns a small dict
    summary. The job is expected to have been created by the caller with
    `total=0` — we update `total` once Scryfall tells us the row count.
    """
    cl = client or get_default_client()
    registry.update(job.id, detail="Fetching bulk-data index…")

    descriptor = cl.find_bulk_descriptor(bulk_type)
    if descriptor is None:
        raise RuntimeError(f"Bulk data type {bulk_type!r} not advertised by Scryfall.")

    download_uri = descriptor.get("download_uri")
    if not download_uri:
        raise RuntimeError("Bulk descriptor missing download_uri.")

    size_bytes = int(descriptor.get("size") or 0)
    job_total_hint = int(descriptor.get("object_count") or 0) or 0
    # Set the job total to estimated card count; the progress bar will read smoothly.
    if job_total_hint:
        live_job = registry.get(job.id)
        if live_job is not None:
            live_job.total = job_total_hint

    registry.update(
        job.id,
        done=0,
        detail=(
            f"Downloading {bulk_type} ({size_bytes / 1_000_000:.0f} MB,"
            f" ~{job_total_hint:,} cards)…"
        ),
    )

    # Download progress, throttled to avoid hammering the registry lock.
    last_pct: list[int] = [-1]

    def _on_download(downloaded: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(100 * downloaded / total)
        if pct != last_pct[0] and pct % 5 == 0:
            registry.update(
                job.id,
                detail=(
                    f"Downloading… {downloaded / 1_000_000:.0f}"
                    f" / {total / 1_000_000:.0f} MB ({pct}%)"
                ),
            )
            last_pct[0] = pct

    # Buffer cards into batches and upsert in chunks of _BATCH_SIZE.
    buffer: list[dict] = []
    processed = [0]
    inserted = [0]
    sets_seen: set[str] = set()

    def _flush() -> None:
        if not buffer:
            return
        upsert_card_dicts(db, buffer, commit=True)
        inserted[0] += len(buffer)
        buffer.clear()

    def _on_card(card: dict) -> None:
        # Skip tokens / art cards / planar dungeons that aren't really "printings"
        # of MTG cards we'd ever import. The default_cards bulk *does* include
        # these, but they're noise for collection matching.
        if card.get("layout") in {"art_series", "token", "double_faced_token"}:
            return
        if card.get("set_type") in {"memorabilia"}:
            # Memorabilia (e.g., gold-bordered) is fine to keep, actually.
            pass
        buffer.append(card)
        processed[0] += 1
        if card.get("set_name"):
            sets_seen.add(card["set_name"])
        if len(buffer) >= _BATCH_SIZE:
            _flush()
            most_recent_set = card.get("set_name") or ""
            registry.update(
                job.id,
                done=processed[0],
                detail=(
                    f"Ingested {processed[0]:,} cards"
                    + (f" · last: {most_recent_set}" if most_recent_set else "")
                ),
            )

    total_parsed = cl.stream_bulk(download_uri, _on_card, progress=_on_download)
    _flush()

    # Final progress bump; make sure done == total even if Scryfall's count drifted.
    final_count = db.scalar(select(func.count(ScryfallCard.scryfall_id))) or 0
    registry.update(
        job.id,
        done=processed[0] or total_parsed,
        detail=(
            f"Done — {inserted[0]:,} cards ingested across"
            f" {len(sets_seen):,} sets · {final_count:,} total in cache"
        ),
    )

    return {
        "parsed": total_parsed,
        "inserted": inserted[0],
        "sets_seen": len(sets_seen),
        "cache_size": int(final_count),
    }


def last_sync_stats(db: Session) -> dict[str, Any]:
    """Tiny helper for the import page header: card count + most recent fetch_at."""
    count = db.scalar(select(func.count(ScryfallCard.scryfall_id))) or 0
    last_fetched = db.scalar(select(func.max(ScryfallCard.fetched_at)))
    return {"card_count": int(count), "last_fetched_at": last_fetched}


__all__ = [
    "bulk_sync",
    "lookup_or_fetch",
    "upsert_card_dict",
    "upsert_card_dicts",
    "last_sync_stats",
]
