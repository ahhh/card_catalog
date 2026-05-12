"""Manabox import: preview + commit.

Two-phase. Preview does *all* Scryfall I/O (cache + live batch resolution),
returns a list of `ImportVerdict` for the UI. Commit is pure SQL, in one
transaction. The verdicts are stashed in a module-level dict so the commit
route can find them by UUID without re-running preview.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from card_catalog.utils import utc_now

from card_catalog.clients.manabox_csv import ImportRow
from card_catalog.clients.scryfall import ScryfallClient, get_default_client
from card_catalog.db.models import CollectionEntry, ImportRun, ScryfallCard
from card_catalog.services import enrich_scryfall

log = logging.getLogger(__name__)


VerdictAction = Literal["insert", "increment", "unmatched"]


@dataclass
class ImportVerdict:
    row: ImportRow
    action: VerdictAction
    scryfall_id: str | None = None
    current_qty: int = 0  # for 'increment'
    new_qty: int = 0  # for 'increment'
    reason: str | None = None  # for 'unmatched'
    card_name: str | None = None
    card_set: str | None = None
    card_set_code: str | None = None
    card_image: str | None = None
    card_collector_number: str | None = None


# ---- in-memory preview cache ---------------------------------------------


@dataclass
class _PreviewBundle:
    id: str
    filename: str | None
    created_at: datetime
    verdicts: list[ImportVerdict]
    warnings: list[str] = field(default_factory=list)


_preview_lock = threading.Lock()
_preview_store: dict[str, _PreviewBundle] = {}
_PREVIEW_CAP = 12  # never hoard more than this many previews in memory


def _stash_preview(bundle: _PreviewBundle) -> None:
    with _preview_lock:
        _preview_store[bundle.id] = bundle
        # Evict oldest if we're over cap.
        if len(_preview_store) > _PREVIEW_CAP:
            oldest = sorted(_preview_store.values(), key=lambda b: b.created_at)[:1]
            for b in oldest:
                _preview_store.pop(b.id, None)


def get_preview(preview_id: str) -> _PreviewBundle | None:
    with _preview_lock:
        return _preview_store.get(preview_id)


def pop_preview(preview_id: str) -> _PreviewBundle | None:
    with _preview_lock:
        return _preview_store.pop(preview_id, None)


# ---- preview --------------------------------------------------------------


def _existing_entries_keyed(
    db: Session, scryfall_ids: list[str]
) -> dict[tuple[str, str, str, str], CollectionEntry]:
    if not scryfall_ids:
        return {}
    rows = (
        db.execute(
            select(CollectionEntry).where(CollectionEntry.scryfall_id.in_(set(scryfall_ids)))
        )
        .scalars()
        .all()
    )
    return {
        (e.scryfall_id, e.finish, e.condition, e.language): e for e in rows
    }


def _fetch_cards_for_rows(
    db: Session,
    rows: list[ImportRow],
    client: ScryfallClient,
) -> dict[str, ScryfallCard]:
    """Resolve cards for every row: cache first, then live batched lookup.

    Returns a dict mapping every resolvable row's *identity* to a ScryfallCard.
    The identity is the row's scryfall_id when present, else
    f"{set_code}|{collector_number}".
    """
    cards: dict[str, ScryfallCard] = {}

    # Step 1 — collect candidate identities.
    sid_rows = [r for r in rows if r.scryfall_id]
    sn_rows = [r for r in rows if not r.scryfall_id and r.set_code and r.collector_number]

    sid_set = {r.scryfall_id for r in sid_rows if r.scryfall_id}
    sn_pairs = {(r.set_code, r.collector_number) for r in sn_rows}

    # Step 2 — cache hits.
    if sid_set:
        for c in (
            db.execute(select(ScryfallCard).where(ScryfallCard.scryfall_id.in_(sid_set)))
            .scalars()
            .all()
        ):
            cards[c.scryfall_id] = c

    if sn_pairs:
        # SQLite doesn't love a tuple IN, so OR them.
        clauses = [
            and_(ScryfallCard.set_code == sc, ScryfallCard.collector_number == cn)
            for (sc, cn) in sn_pairs
        ]
        if clauses:
            for c in db.execute(select(ScryfallCard).where(or_(*clauses))).scalars().all():
                cards[f"{c.set_code}|{c.collector_number}"] = c

    # Step 3 — gather still-missing identifiers for a live batch.
    missing_ids: list[dict] = []
    seen_ids: set[str] = set()
    seen_sn: set[tuple[str, str]] = set()
    for r in sid_rows:
        if r.scryfall_id and r.scryfall_id not in cards and r.scryfall_id not in seen_ids:
            missing_ids.append({"id": r.scryfall_id})
            seen_ids.add(r.scryfall_id)
    for r in sn_rows:
        key = f"{r.set_code}|{r.collector_number}"
        pair = (r.set_code, r.collector_number)
        if key not in cards and pair not in seen_sn:
            missing_ids.append({"set": r.set_code, "collector_number": r.collector_number})
            seen_sn.add(pair)

    if missing_ids and client is not None:
        try:
            result = client.get_collection(missing_ids)
        except Exception as exc:  # noqa: BLE001
            log.warning("scryfall /cards/collection failed: %s", exc)
            result = {"found": [], "not_found": []}

        found = result.get("found") or []
        if found:
            # Persist to cache, then re-index.
            enrich_scryfall.upsert_card_dicts(db, found, commit=True)
            for card_dict in found:
                sid = card_dict.get("id")
                if sid:
                    persisted = db.get(ScryfallCard, sid)
                    if persisted is None:
                        continue
                    cards[persisted.scryfall_id] = persisted
                    cards[f"{persisted.set_code}|{persisted.collector_number}"] = persisted

    return cards


def preview(
    db: Session,
    rows: list[ImportRow],
    *,
    scryfall_client: ScryfallClient | None = None,
    filename: str | None = None,
    warnings: list[str] | None = None,
) -> tuple[str, list[ImportVerdict], _PreviewBundle]:
    """Produce verdicts for every row and stash them under a preview_id.

    Returns (preview_id, verdicts, bundle).
    """
    if not rows:
        bundle = _PreviewBundle(
            id=uuid.uuid4().hex,
            filename=filename,
            created_at=utc_now(),
            verdicts=[],
            warnings=list(warnings or []),
        )
        _stash_preview(bundle)
        return bundle.id, [], bundle

    client = scryfall_client if scryfall_client is not None else get_default_client()
    cards_index = _fetch_cards_for_rows(db, rows, client)

    # Resolve every row to a scryfall_id (or None for unmatched).
    resolved_ids: list[str] = []
    matches: list[ScryfallCard | None] = []
    for r in rows:
        card: ScryfallCard | None = None
        if r.scryfall_id and r.scryfall_id in cards_index:
            card = cards_index[r.scryfall_id]
        elif r.set_code and r.collector_number:
            card = cards_index.get(f"{r.set_code}|{r.collector_number}")
        matches.append(card)
        if card is not None:
            resolved_ids.append(card.scryfall_id)

    # Existing entries — for increment detection.
    existing = _existing_entries_keyed(db, resolved_ids)

    verdicts: list[ImportVerdict] = []
    for row, card in zip(rows, matches, strict=False):
        if card is None:
            reason = "Unknown Scryfall ID" if row.scryfall_id else "Set + number not found"
            verdicts.append(
                ImportVerdict(
                    row=row,
                    action="unmatched",
                    reason=reason,
                    card_name=row.name or None,
                )
            )
            continue

        key = (card.scryfall_id, row.finish, row.condition, row.language)
        existing_entry = existing.get(key)
        if existing_entry is not None:
            verdicts.append(
                ImportVerdict(
                    row=row,
                    action="increment",
                    scryfall_id=card.scryfall_id,
                    current_qty=int(existing_entry.quantity),
                    new_qty=int(existing_entry.quantity) + int(row.quantity),
                    card_name=card.name,
                    card_set=card.set_name,
                    card_set_code=card.set_code,
                    card_image=card.image_small_uri,
                    card_collector_number=card.collector_number,
                )
            )
        else:
            verdicts.append(
                ImportVerdict(
                    row=row,
                    action="insert",
                    scryfall_id=card.scryfall_id,
                    new_qty=int(row.quantity),
                    card_name=card.name,
                    card_set=card.set_name,
                    card_set_code=card.set_code,
                    card_image=card.image_small_uri,
                    card_collector_number=card.collector_number,
                )
            )

    bundle = _PreviewBundle(
        id=uuid.uuid4().hex,
        filename=filename,
        created_at=utc_now(),
        verdicts=verdicts,
        warnings=list(warnings or []),
    )
    _stash_preview(bundle)
    return bundle.id, verdicts, bundle


# ---- commit --------------------------------------------------------------


@dataclass
class CommitResult:
    import_run_id: int
    rows_total: int
    rows_imported: int  # inserts + increments (cards actually persisted)
    rows_skipped: int
    rows_unmatched: int
    inserted: int
    incremented: int


def commit(
    db: Session,
    preview_id: str,
    *,
    consume: bool = True,
) -> CommitResult:
    """Apply a previously-staged preview to the DB. One transaction."""
    bundle = pop_preview(preview_id) if consume else get_preview(preview_id)
    if bundle is None:
        raise LookupError(f"Preview {preview_id} not found or expired.")

    verdicts = bundle.verdicts
    rows_total = len(verdicts)
    inserted = 0
    incremented = 0
    unmatched = 0

    run = ImportRun(
        source="manabox",
        filename=bundle.filename,
        started_at=utc_now(),
        rows_total=rows_total,
    )
    db.add(run)
    db.flush()

    try:
        for v in verdicts:
            if v.action == "unmatched":
                unmatched += 1
                continue
            if not v.scryfall_id:
                unmatched += 1
                continue

            if v.action == "insert":
                entry = CollectionEntry(
                    scryfall_id=v.scryfall_id,
                    finish=v.row.finish,
                    condition=v.row.condition,
                    language=v.row.language,
                    quantity=int(v.row.quantity),
                    purchase_price=v.row.purchase_price,
                    purchase_currency=v.row.purchase_currency,
                    altered=1 if v.row.altered else 0,
                    misprint=1 if v.row.misprint else 0,
                )
                db.add(entry)
                inserted += 1
            elif v.action == "increment":
                key = (v.scryfall_id, v.row.finish, v.row.condition, v.row.language)
                existing = db.scalars(
                    select(CollectionEntry).where(
                        CollectionEntry.scryfall_id == key[0],
                        CollectionEntry.finish == key[1],
                        CollectionEntry.condition == key[2],
                        CollectionEntry.language == key[3],
                    )
                ).first()
                if existing is None:
                    # Race: someone deleted between preview and commit. Insert instead.
                    entry = CollectionEntry(
                        scryfall_id=v.scryfall_id,
                        finish=v.row.finish,
                        condition=v.row.condition,
                        language=v.row.language,
                        quantity=int(v.row.quantity),
                        purchase_price=v.row.purchase_price,
                        purchase_currency=v.row.purchase_currency,
                        altered=1 if v.row.altered else 0,
                        misprint=1 if v.row.misprint else 0,
                    )
                    db.add(entry)
                    inserted += 1
                else:
                    existing.quantity = int(existing.quantity) + int(v.row.quantity)
                    incremented += 1

        run.rows_imported = inserted + incremented
        run.rows_skipped = 0
        run.rows_unmatched = unmatched
        run.finished_at = utc_now()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        # `run` was rolled back too — recreate a failure-audit row in a fresh tx.
        fail_run = ImportRun(
            source="manabox",
            filename=bundle.filename,
            started_at=utc_now(),
            finished_at=utc_now(),
            rows_total=rows_total,
            rows_imported=0,
            rows_unmatched=unmatched,
            error=str(exc),
        )
        db.add(fail_run)
        db.commit()
        raise

    return CommitResult(
        import_run_id=int(run.id),
        rows_total=rows_total,
        rows_imported=inserted + incremented,
        rows_skipped=0,
        rows_unmatched=unmatched,
        inserted=inserted,
        incremented=incremented,
    )


__all__ = [
    "ImportVerdict",
    "CommitResult",
    "preview",
    "commit",
    "get_preview",
    "pop_preview",
]
