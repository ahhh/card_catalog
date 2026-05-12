"""Archidekt services: CSV export of the local collection, deck reconciliation.

The HTTP layer (routers/archidekt.py) is intentionally thin — these two
functions own the business logic for both flows.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from card_catalog.clients.archidekt import (
    FetchedDeck,
    FetchedDeckCard,
    export_to_csv,
)
from card_catalog.db.models import CollectionEntry, PriceHistory, ScryfallCard


# ---- Export ----------------------------------------------------------------


def export_filtered_collection(
    db: Session, filter_spec_dict: dict | None = None
) -> tuple[str, str]:
    """Return ``(csv_string, suggested_filename)`` for the Archidekt importer.

    Aggregation: ``GROUP BY scryfall_id, SUM(quantity)`` — Archidekt's
    minimum import format is ``Quantity, Scryfall ID`` and does not
    distinguish finish/condition/language, so we sum across all of those.

    ``filter_spec_dict`` is accepted for forward-compatibility (M8 plan item)
    but currently ignored — we export the full collection. Wiring the filter
    spec through ``services/collection.py:search()`` is deferred until that
    service exists.
    """
    rows_q = (
        select(
            CollectionEntry.scryfall_id,
            func.sum(CollectionEntry.quantity),
        )
        .group_by(CollectionEntry.scryfall_id)
        .order_by(CollectionEntry.scryfall_id)
    )
    rows: Iterable[tuple[int, str]] = (
        (int(qty or 0), sid) for sid, qty in db.execute(rows_q).all() if sid and (qty or 0) > 0
    )

    buf = io.StringIO()
    export_to_csv(rows, buf)
    today = date.today().isoformat()
    return buf.getvalue(), f"card-catalog-export-{today}.csv"


# ---- Reconcile -------------------------------------------------------------


@dataclass
class OwnedPrinting:
    """A single printing we own, surfaced in the reconcile UI."""

    scryfall_id: str
    set_code: str
    collector_number: str
    quantity: int
    finish: str
    image_small_uri: str | None


@dataclass
class ReconcileRow:
    deck_card: FetchedDeckCard
    needed_qty: int
    owned_qty: int
    missing_qty: int
    cheapest_market_price: float | None  # for ONE missing copy, if known
    image_uri: str | None
    matched_scryfall_id: str | None
    owned_printings: list[OwnedPrinting]


@dataclass
class ReconcileReport:
    deck: FetchedDeck
    rows: list[ReconcileRow]
    total_needed: int
    total_owned: int
    total_missing: int
    est_completion_cost: float | None


def _latest_market_price(db: Session, tcgplayer_id: int | None) -> float | None:
    """Cheapest meaningful market price for one copy at this product (Normal sub_type)."""
    if not tcgplayer_id:
        return None
    row = db.execute(
        select(PriceHistory.market_price, PriceHistory.mid_price, PriceHistory.low_price)
        .where(PriceHistory.tcgplayer_id == tcgplayer_id)
        .where(PriceHistory.sub_type == "Normal")
        .order_by(PriceHistory.as_of.desc())
        .limit(1)
    ).first()
    if not row:
        return None
    market, mid, low = row
    for v in (market, mid, low):
        if v is not None:
            return float(v)
    return None


def _resolve_oracle_id(db: Session, scryfall_id: str | None) -> str | None:
    if not scryfall_id:
        return None
    return db.scalar(
        select(ScryfallCard.oracle_id).where(ScryfallCard.scryfall_id == scryfall_id)
    )


def _printings_for_oracle(db: Session, oracle_id: str) -> list[ScryfallCard]:
    return list(
        db.execute(
            select(ScryfallCard).where(ScryfallCard.oracle_id == oracle_id)
        ).scalars()
    )


def _printing_by_scryfall_id(db: Session, scryfall_id: str) -> ScryfallCard | None:
    return db.get(ScryfallCard, scryfall_id)


def _owned_for_printings(
    db: Session, scryfall_ids: list[str]
) -> tuple[list[OwnedPrinting], int]:
    """Return (per-printing owned breakdown, total quantity)."""
    if not scryfall_ids:
        return [], 0
    rows = db.execute(
        select(
            CollectionEntry.scryfall_id,
            CollectionEntry.finish,
            func.sum(CollectionEntry.quantity),
            ScryfallCard.set_code,
            ScryfallCard.collector_number,
            ScryfallCard.image_small_uri,
        )
        .join(ScryfallCard, ScryfallCard.scryfall_id == CollectionEntry.scryfall_id)
        .where(CollectionEntry.scryfall_id.in_(scryfall_ids))
        .group_by(
            CollectionEntry.scryfall_id,
            CollectionEntry.finish,
            ScryfallCard.set_code,
            ScryfallCard.collector_number,
            ScryfallCard.image_small_uri,
        )
    ).all()

    owned: list[OwnedPrinting] = []
    total = 0
    for sid, finish, qty, set_code, coll, img in rows:
        q = int(qty or 0)
        if q <= 0:
            continue
        total += q
        owned.append(
            OwnedPrinting(
                scryfall_id=sid,
                set_code=set_code or "",
                collector_number=coll or "",
                quantity=q,
                finish=finish or "nonfoil",
                image_small_uri=img,
            )
        )
    # Sort: highest quantity first, then by set_code for stability.
    owned.sort(key=lambda p: (-p.quantity, p.set_code))
    return owned, total


def _pick_thumbnail(printings: list[ScryfallCard]) -> str | None:
    """Prefer the first printing that actually has an image URL."""
    for p in printings:
        if p.image_normal_uri:
            return p.image_normal_uri
        if p.image_small_uri:
            return p.image_small_uri
    return None


def _cheapest_unowned_price(
    db: Session, printings: list[ScryfallCard], owned_sids: set[str]
) -> float | None:
    """Cheapest latest market price across printings we DON'T already own.

    Falls back to any printing if every printing is already owned (e.g. user
    wants to round-trip purchase a duplicate). Returns None when no price
    data is available at all.
    """
    candidates = [p for p in printings if p.scryfall_id not in owned_sids] or printings
    best: float | None = None
    for p in candidates:
        tid = p.tcgplayer_id or p.tcgplayer_etched_id
        price = _latest_market_price(db, tid)
        if price is None:
            continue
        if best is None or price < best:
            best = price
    return best


def reconcile_with_deck(db: Session, deck: FetchedDeck) -> ReconcileReport:
    """Compute owned/needed/missing per deck card against the local collection.

    Matching strategy, per card:
      1. If the deck card has a Scryfall id and we have it in ScryfallCard,
         resolve its oracle_id and count owned across ALL printings sharing
         that oracle (i.e. owning a different printing still satisfies the slot).
      2. Else if we know the printing directly (scryfall_id match), count
         only its own copies.
      3. Else: zero owned, all missing.

    Estimated cost-to-complete uses the cheapest available market price
    across printings we don't already own.
    """
    rows: list[ReconcileRow] = []
    total_needed = 0
    total_owned = 0
    total_missing = 0
    est_total: float = 0.0
    any_price_seen = False

    for deck_card in deck.cards:
        needed = max(int(deck_card.quantity or 0), 0)
        if needed == 0:
            # Silently skip zero-qty entries (rare, but Archidekt allows them).
            continue
        total_needed += needed

        oracle_id = _resolve_oracle_id(db, deck_card.scryfall_id)
        printings: list[ScryfallCard] = []
        matched_scryfall_id: str | None = None

        if oracle_id:
            printings = _printings_for_oracle(db, oracle_id)
            matched_scryfall_id = deck_card.scryfall_id
            # Mutate the dataclass with the resolved oracle_id so callers/tests
            # can introspect it later.
            deck_card.oracle_id = oracle_id
        elif deck_card.scryfall_id:
            direct = _printing_by_scryfall_id(db, deck_card.scryfall_id)
            if direct is not None:
                printings = [direct]
                matched_scryfall_id = direct.scryfall_id
                deck_card.oracle_id = direct.oracle_id

        printing_ids = [p.scryfall_id for p in printings]
        owned_printings, owned_qty = _owned_for_printings(db, printing_ids)
        owned_capped = min(owned_qty, needed)
        total_owned += owned_capped
        missing = max(needed - owned_qty, 0)
        total_missing += missing

        cheapest = _cheapest_unowned_price(
            db, printings, {op.scryfall_id for op in owned_printings}
        )
        if cheapest is not None:
            any_price_seen = True
            est_total += cheapest * missing

        # Pick a thumbnail. Prefer an owned printing's image if available so
        # the UI feels personal; otherwise any printing.
        thumb: str | None = None
        for op in owned_printings:
            if op.image_small_uri:
                thumb = op.image_small_uri
                break
        if not thumb:
            thumb = _pick_thumbnail(printings)

        rows.append(
            ReconcileRow(
                deck_card=deck_card,
                needed_qty=needed,
                owned_qty=owned_qty,
                missing_qty=missing,
                cheapest_market_price=cheapest,
                image_uri=thumb,
                matched_scryfall_id=matched_scryfall_id,
                owned_printings=owned_printings,
            )
        )

    # Stable, useful sort: missing-first (biggest gap), then by name.
    rows.sort(key=lambda r: (-r.missing_qty, r.deck_card.name.lower()))

    return ReconcileReport(
        deck=deck,
        rows=rows,
        total_needed=total_needed,
        total_owned=total_owned,
        total_missing=total_missing,
        est_completion_cost=est_total if any_price_seen else None,
    )
