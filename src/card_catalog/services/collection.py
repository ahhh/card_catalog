"""Collection browse, filter, sort, paginate + edit/bulk-edit.

The search query joins CollectionEntry → ScryfallCard, LEFT JOINs the latest
PriceHistory row per (tcgplayer_id, sub_type) using a correlated-max subquery
(SQLite has window functions but the correlated approach is just as fast and
clearer here). The colors filter is JSON-string LIKE-matched — coarse but
unavoidable on SQLite with a JSON-as-TEXT column.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.orm import Session, contains_eager, selectinload

from card_catalog.utils import utc_now

from card_catalog.db.models import (
    CollectionEntry,
    EntryTag,
    PriceHistory,
    ScryfallCard,
    Tag,
)
from card_catalog.domain.enums import Condition, Finish


# ---- Filter / Search --------------------------------------------------------


ColorsMode = Literal["includes", "exact", "identity"]


SORT_WHITELIST: dict[str, Any] = {
    "name": ScryfallCard.name,
    "set_code": ScryfallCard.set_code,
    "set": ScryfallCard.set_code,
    "cmc": ScryfallCard.cmc,
    "quantity": CollectionEntry.quantity,
    "qty": CollectionEntry.quantity,
    "condition": case(
        {"NM": 1, "LP": 2, "MP": 3, "HP": 4, "DMG": 5},
        value=CollectionEntry.condition,
        else_=99,
    ),
    "finish": CollectionEntry.finish,
    "language": CollectionEntry.language,
    "created_at": CollectionEntry.created_at,
    "created": CollectionEntry.created_at,
    "rarity": case(
        {"common": 1, "uncommon": 2, "rare": 3, "mythic": 4, "special": 5, "bonus": 6},
        value=ScryfallCard.rarity,
        else_=0,
    ),
}


class FilterSpec(BaseModel):
    """All filter / sort / paginate params for the collection browse."""

    q: str | None = None
    set_code: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    colors_mode: ColorsMode = "includes"
    rarity: list[str] = Field(default_factory=list)
    cmc_min: float | None = None
    cmc_max: float | None = None
    price_min: float | None = None
    price_max: float | None = None
    qty_min: int | None = None
    qty_max: int | None = None
    finish: list[str] = Field(default_factory=list)
    condition: list[str] = Field(default_factory=list)
    language: list[str] = Field(default_factory=list)
    for_trade: bool | None = None
    tags: list[int] = Field(default_factory=list)
    sort: str = "name"
    page: int = 1
    page_size: int = 60

    @field_validator(
        "set_code", "colors", "rarity", "finish", "condition", "language", mode="before"
    )
    @classmethod
    def _split_csv(cls, v: Any) -> list[str]:
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return []

    @field_validator("tags", mode="before")
    @classmethod
    def _split_int_csv(cls, v: Any) -> list[int]:
        if v is None or v == "":
            return []
        if isinstance(v, list):
            out: list[int] = []
            for x in v:
                try:
                    out.append(int(x))
                except (TypeError, ValueError):
                    pass
            return out
        if isinstance(v, str):
            return [int(s) for s in v.split(",") if s.strip().lstrip("-").isdigit()]
        return []

    @field_validator("page", mode="before")
    @classmethod
    def _coerce_page(cls, v: Any) -> int:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 1
        return max(1, n)

    @field_validator("page_size", mode="before")
    @classmethod
    def _coerce_page_size(cls, v: Any) -> int:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 60
        return max(1, min(n, 240))

    @field_validator("for_trade", mode="before")
    @classmethod
    def _coerce_bool(cls, v: Any) -> bool | None:
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
        return None

    def querystring(self) -> str:
        """Reproducible querystring for HX-Push-Url and pager links."""
        from urllib.parse import urlencode

        parts: list[tuple[str, str]] = []
        if self.q:
            parts.append(("q", self.q))
        for k in ("set_code", "colors", "rarity", "finish", "condition", "language"):
            for v in getattr(self, k):
                parts.append((k, v))
        for k in ("cmc_min", "cmc_max", "price_min", "price_max", "qty_min", "qty_max"):
            v = getattr(self, k)
            if v is not None:
                parts.append((k, str(v)))
        if self.for_trade is not None:
            parts.append(("for_trade", "1" if self.for_trade else "0"))
        for t in self.tags:
            parts.append(("tags", str(t)))
        if self.colors and self.colors_mode != "includes":
            parts.append(("colors_mode", self.colors_mode))
        if self.sort and self.sort != "name":
            parts.append(("sort", self.sort))
        if self.page > 1:
            parts.append(("page", str(self.page)))
        if self.page_size != 60:
            parts.append(("page_size", str(self.page_size)))
        return urlencode(parts)


@dataclass
class SearchResult:
    entries: list[CollectionEntry]
    latest_prices: dict[int, float]
    total: int
    page: int
    page_size: int
    filt: "FilterSpec"
    total_quantity: int = 0
    total_value: float = 0.0

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, (self.total + self.page_size - 1) // self.page_size)


def _sub_type_expr() -> Any:
    """SQL expression producing 'Foil'|'Foil Etched'|'Normal' from finish."""
    return case(
        {"foil": "Foil", "etched": "Foil Etched"},
        value=CollectionEntry.finish,
        else_="Normal",
    )


def _tcg_id_expr() -> Any:
    """Picks etched_id when finish='etched', else the normal tcgplayer_id."""
    return case(
        (CollectionEntry.finish == "etched", ScryfallCard.tcgplayer_etched_id),
        else_=ScryfallCard.tcgplayer_id,
    )


def _latest_price_subquery():
    return (
        select(
            PriceHistory.tcgplayer_id,
            PriceHistory.sub_type,
            func.max(PriceHistory.as_of).label("as_of"),
        )
        .group_by(PriceHistory.tcgplayer_id, PriceHistory.sub_type)
        .subquery()
    )


def _apply_filters(stmt, filt: FilterSpec):
    """Apply non-price filters that are valid for both the count and select queries."""
    if filt.q:
        like = f"%{filt.q.strip()}%"
        stmt = stmt.where(
            or_(
                ScryfallCard.name.ilike(like),
                ScryfallCard.type_line.ilike(like),
                ScryfallCard.oracle_text.ilike(like),
                ScryfallCard.set_name.ilike(like),
            )
        )
    if filt.set_code:
        codes = [c.lower() for c in filt.set_code]
        stmt = stmt.where(ScryfallCard.set_code.in_(codes))
    if filt.rarity:
        stmt = stmt.where(ScryfallCard.rarity.in_([r.lower() for r in filt.rarity]))
    if filt.finish:
        stmt = stmt.where(CollectionEntry.finish.in_(filt.finish))
    if filt.condition:
        stmt = stmt.where(CollectionEntry.condition.in_(filt.condition))
    if filt.language:
        stmt = stmt.where(CollectionEntry.language.in_(filt.language))
    if filt.cmc_min is not None:
        stmt = stmt.where(ScryfallCard.cmc >= filt.cmc_min)
    if filt.cmc_max is not None:
        stmt = stmt.where(ScryfallCard.cmc <= filt.cmc_max)
    if filt.qty_min is not None:
        stmt = stmt.where(CollectionEntry.quantity >= filt.qty_min)
    if filt.qty_max is not None:
        stmt = stmt.where(CollectionEntry.quantity <= filt.qty_max)
    if filt.for_trade is True:
        stmt = stmt.where(CollectionEntry.for_trade == 1)
    elif filt.for_trade is False:
        stmt = stmt.where(CollectionEntry.for_trade == 0)

    if filt.colors:
        col = ScryfallCard.colors if filt.colors_mode != "identity" else ScryfallCard.color_identity
        wanted = [c.upper() for c in filt.colors if c.upper() in {"W", "U", "B", "R", "G"}]
        if filt.colors_mode == "exact":
            # Match exactly this color set in any order. Scryfall emits
            # ["U","R"] (default json.dumps separators). Enumerating
            # permutations for ≤5 colors is cheap (max 120) and lets us hit
            # an equality with a small IN list.
            from itertools import permutations

            patterns = [json.dumps(list(p)) for p in permutations(wanted)]
            stmt = stmt.where(col.in_(patterns))
        else:
            # 'includes' and 'identity' both: the card has at least every wanted color.
            for c in wanted:
                stmt = stmt.where(col.like(f'%"{c}"%'))

    if filt.tags:
        stmt = stmt.where(
            CollectionEntry.id.in_(
                select(EntryTag.entry_id).where(EntryTag.tag_id.in_(filt.tags))
            )
        )

    return stmt


def _price_join(stmt, latest):
    """LEFT JOIN PriceHistory + the latest subquery onto the current statement.
    Joined entity is the same PriceHistory table; we filter via latest's date.
    """
    return stmt.join(
        PriceHistory,
        and_(
            PriceHistory.tcgplayer_id == _tcg_id_expr(),
            PriceHistory.sub_type == _sub_type_expr(),
        ),
        isouter=True,
    ).join(
        latest,
        and_(
            latest.c.tcgplayer_id == PriceHistory.tcgplayer_id,
            latest.c.sub_type == PriceHistory.sub_type,
            latest.c.as_of == PriceHistory.as_of,
        ),
        isouter=True,
    )


def _market_price_expr() -> Any:
    return func.coalesce(PriceHistory.market_price, PriceHistory.mid_price)


def search(db: Session, filt: FilterSpec) -> SearchResult:
    """Search the collection. Returns hydrated entries plus a price map."""
    latest = _latest_price_subquery()

    # Base query: entries + cards. Price columns brought in via the LEFT JOIN so
    # we can filter on them and sort by them in SQL.
    base = (
        select(
            CollectionEntry.id.label("entry_id"),
            _market_price_expr().label("market_price"),
        )
        .select_from(CollectionEntry)
        .join(ScryfallCard, ScryfallCard.scryfall_id == CollectionEntry.scryfall_id)
    )
    base = _price_join(base, latest)
    base = _apply_filters(base, filt)

    if filt.price_min is not None:
        base = base.where(_market_price_expr() >= filt.price_min)
    if filt.price_max is not None:
        base = base.where(_market_price_expr() <= filt.price_max)

    # Count: distinct entry ids matching the predicate set.
    count_stmt = select(func.count()).select_from(base.subquery())
    total = db.scalar(count_stmt) or 0

    # Aggregate quantity + value across the *full* filtered set (not just the page),
    # so the page header subtitle reflects the filter, not the world.
    agg_subq = base.subquery()
    qty_value_stmt = (
        select(
            func.coalesce(func.sum(CollectionEntry.quantity), 0),
            func.coalesce(
                func.sum(CollectionEntry.quantity * func.coalesce(agg_subq.c.market_price, 0.0)),
                0.0,
            ),
        )
        .select_from(CollectionEntry)
        .join(agg_subq, agg_subq.c.entry_id == CollectionEntry.id)
    )
    total_qty, total_value = db.execute(qty_value_stmt).one()

    # Page query: same predicate set, sort + limit/offset.
    sort_key = filt.sort or "name"
    descending = sort_key.startswith("-")
    sort_name = sort_key.lstrip("-")
    if sort_name == "price":
        sort_col = _market_price_expr()
    else:
        sort_col = SORT_WHITELIST.get(sort_name, ScryfallCard.name)
    order_clause = sort_col.desc() if descending else sort_col.asc()
    # Tiebreakers: stable ordering by entry id for pagination determinism.
    tiebreaker = ScryfallCard.name.asc() if sort_name != "name" else None

    page_stmt = (
        select(CollectionEntry, _market_price_expr().label("market_price"))
        .select_from(CollectionEntry)
        .join(ScryfallCard, ScryfallCard.scryfall_id == CollectionEntry.scryfall_id)
        .options(
            contains_eager(CollectionEntry.card),
            selectinload(CollectionEntry.tags),
        )
    )
    page_stmt = _price_join(page_stmt, latest)
    page_stmt = _apply_filters(page_stmt, filt)
    if filt.price_min is not None:
        page_stmt = page_stmt.where(_market_price_expr() >= filt.price_min)
    if filt.price_max is not None:
        page_stmt = page_stmt.where(_market_price_expr() <= filt.price_max)
    page_stmt = page_stmt.order_by(order_clause)
    if tiebreaker is not None:
        page_stmt = page_stmt.order_by(tiebreaker)
    page_stmt = page_stmt.order_by(CollectionEntry.id.asc())
    page_stmt = page_stmt.offset((filt.page - 1) * filt.page_size).limit(filt.page_size)

    rows = db.execute(page_stmt).all()
    entries = [r[0] for r in rows]
    prices: dict[int, float] = {}
    for entry, market in rows:
        if market is not None:
            try:
                prices[entry.id] = float(market)
            except (TypeError, ValueError):
                pass

    return SearchResult(
        entries=entries,
        latest_prices=prices,
        total=int(total),
        page=filt.page,
        page_size=filt.page_size,
        filt=filt,
        total_quantity=int(total_qty or 0),
        total_value=float(total_value or 0.0),
    )


# ---- Detail-page helpers ----------------------------------------------------


def get_card(db: Session, scryfall_id: str) -> ScryfallCard | None:
    return db.get(ScryfallCard, scryfall_id)


def get_entries_for_card(db: Session, scryfall_id: str) -> list[CollectionEntry]:
    stmt = (
        select(CollectionEntry)
        .where(CollectionEntry.scryfall_id == scryfall_id)
        .options(selectinload(CollectionEntry.tags))
        .order_by(CollectionEntry.finish, CollectionEntry.condition, CollectionEntry.language)
    )
    return list(db.scalars(stmt).all())


def latest_price_for_card(
    db: Session, card: ScryfallCard, finish: str = "nonfoil"
) -> dict[str, Any] | None:
    """Returns dict with keys: as_of, low, mid, high, market, direct_low.
    None if no tcgplayer mapping or no rows."""
    tcg_id = card.tcgplayer_etched_id if finish == "etched" else card.tcgplayer_id
    if tcg_id is None:
        return None
    sub_type = {"foil": "Foil", "etched": "Foil Etched"}.get(finish, "Normal")
    stmt = (
        select(PriceHistory)
        .where(
            PriceHistory.tcgplayer_id == tcg_id,
            PriceHistory.sub_type == sub_type,
        )
        .order_by(PriceHistory.as_of.desc())
        .limit(1)
    )
    row = db.scalars(stmt).first()
    if row is None:
        return None
    return {
        "as_of": row.as_of,
        "low": row.low_price,
        "mid": row.mid_price,
        "high": row.high_price,
        "market": row.market_price,
        "direct_low": row.direct_low_price,
    }


def price_history_points(
    db: Session, card: ScryfallCard, finish: str = "nonfoil", days: int = 30
) -> list[float]:
    """Recent market-price points for the sparkline, oldest → newest."""
    tcg_id = card.tcgplayer_etched_id if finish == "etched" else card.tcgplayer_id
    if tcg_id is None:
        return []
    sub_type = {"foil": "Foil", "etched": "Foil Etched"}.get(finish, "Normal")
    stmt = (
        select(PriceHistory.market_price, PriceHistory.mid_price)
        .where(
            PriceHistory.tcgplayer_id == tcg_id,
            PriceHistory.sub_type == sub_type,
        )
        .order_by(PriceHistory.as_of.asc())
        .limit(days)
    )
    out: list[float] = []
    for market, mid in db.execute(stmt).all():
        v = market if market is not None else mid
        if v is not None:
            out.append(float(v))
    return out


# ---- Editing ----------------------------------------------------------------


_EDITABLE_FIELDS = {
    "quantity",
    "condition",
    "finish",
    "language",
    "notes",
    "for_trade",
    "altered",
    "misprint",
    "purchase_price",
    "purchase_currency",
    "purchase_date",
}


class EditError(ValueError):
    pass


def _coerce_field(name: str, value: Any) -> Any:
    """Coerce + validate a single field value. Raises EditError on bad input."""
    if value is None:
        if name in {"purchase_price", "purchase_currency", "purchase_date", "notes"}:
            return None
        raise EditError(f"{name} cannot be empty")
    if isinstance(value, str):
        value = value.strip()
        if value == "" and name in {
            "purchase_price", "purchase_currency", "purchase_date", "notes"
        }:
            return None

    if name == "quantity":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise EditError("quantity must be an integer")
        if n < 0:
            raise EditError("quantity cannot be negative")
        return n
    if name == "condition":
        try:
            return Condition(str(value).upper()).value
        except ValueError:
            raise EditError(f"unknown condition '{value}'")
    if name == "finish":
        try:
            return Finish(str(value).lower()).value
        except ValueError:
            raise EditError(f"unknown finish '{value}'")
    if name == "language":
        s = str(value).strip().lower()
        return s[:8] or "en"
    if name in {"for_trade", "altered", "misprint"}:
        if isinstance(value, bool):
            return 1 if value else 0
        s = str(value).strip().lower()
        return 1 if s in {"1", "true", "yes", "y", "on"} else 0
    if name == "purchase_price":
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise EditError("purchase_price must be numeric")
    if name == "purchase_currency":
        if value in (None, ""):
            return None
        return str(value).strip().upper()[:8]
    if name == "purchase_date":
        if value in (None, ""):
            return None
        if isinstance(value, date):
            return value
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            raise EditError("purchase_date must be YYYY-MM-DD")
    if name == "notes":
        return None if value in (None, "") else str(value)[:4000]
    return value


def _filter_patch(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown keys and coerce values."""
    out: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in _EDITABLE_FIELDS:
            continue
        out[k] = _coerce_field(k, v)
    return out


def update_entry(
    db: Session, entry_id: int, fields: dict[str, Any]
) -> CollectionEntry | None:
    """Patch a single entry. Returns the updated entry, or None if it was deleted
    (quantity went to 0). Raises EditError on bad input, KeyError if missing.
    """
    entry = db.get(CollectionEntry, entry_id)
    if entry is None:
        raise KeyError(entry_id)
    patch = _filter_patch(fields)
    # quantity=0 → delete
    if patch.get("quantity") == 0:
        db.delete(entry)
        db.commit()
        return None
    for k, v in patch.items():
        setattr(entry, k, v)
    entry.updated_at = utc_now()
    db.commit()
    db.refresh(entry)
    return entry


def bulk_update(db: Session, entry_ids: Iterable[int], fields: dict[str, Any]) -> int:
    """Apply the same patch to N entries. Returns count affected. Skips quantity=0."""
    ids = [int(i) for i in entry_ids if str(i).strip()]
    if not ids:
        return 0
    patch = _filter_patch(fields)
    # Bulk-deleting via quantity=0 is dangerous; require explicit delete_entries.
    patch.pop("quantity", None)
    if not patch:
        return 0
    rows = db.execute(
        select(CollectionEntry).where(CollectionEntry.id.in_(ids))
    ).scalars().all()
    n = 0
    for entry in rows:
        for k, v in patch.items():
            setattr(entry, k, v)
        entry.updated_at = utc_now()
        n += 1
    db.commit()
    return n


def delete_entry(db: Session, entry_id: int) -> None:
    entry = db.get(CollectionEntry, entry_id)
    if entry is None:
        raise KeyError(entry_id)
    db.delete(entry)
    db.commit()


def bulk_delete(db: Session, entry_ids: Iterable[int]) -> int:
    ids = [int(i) for i in entry_ids if str(i).strip()]
    if not ids:
        return 0
    result = db.execute(delete(CollectionEntry).where(CollectionEntry.id.in_(ids)))
    db.commit()
    return result.rowcount or 0


# ---- Tags -------------------------------------------------------------------


def list_tags(db: Session) -> list[Tag]:
    return list(db.scalars(select(Tag).order_by(Tag.name.asc())).all())


def _get_or_create_tag(db: Session, name: str) -> Tag:
    name = name.strip()
    tag = db.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        tag = Tag(name=name)
        db.add(tag)
        db.flush()
    return tag


def add_tag(db: Session, entry_id: int, tag_name: str) -> Tag:
    entry = db.get(CollectionEntry, entry_id)
    if entry is None:
        raise KeyError(entry_id)
    tag = _get_or_create_tag(db, tag_name)
    if tag not in entry.tags:
        entry.tags.append(tag)
    db.commit()
    return tag


def remove_tag(db: Session, entry_id: int, tag_id: int) -> None:
    entry = db.get(CollectionEntry, entry_id)
    if entry is None:
        raise KeyError(entry_id)
    entry.tags = [t for t in entry.tags if t.id != tag_id]
    db.commit()


def bulk_add_tag(db: Session, entry_ids: Iterable[int], tag_name: str) -> int:
    ids = [int(i) for i in entry_ids if str(i).strip()]
    if not ids:
        return 0
    tag = _get_or_create_tag(db, tag_name)
    entries = db.execute(
        select(CollectionEntry).where(CollectionEntry.id.in_(ids))
    ).scalars().all()
    n = 0
    for entry in entries:
        if tag not in entry.tags:
            entry.tags.append(tag)
            n += 1
    db.commit()
    return n


# ---- Facet helpers ----------------------------------------------------------


def distinct_sets(db: Session, limit: int = 100) -> list[tuple[str, str, int]]:
    """Sets present in the collection, with entry-count. Returns (code, name, count)."""
    stmt = (
        select(
            ScryfallCard.set_code,
            ScryfallCard.set_name,
            func.count(CollectionEntry.id),
        )
        .select_from(CollectionEntry)
        .join(ScryfallCard, ScryfallCard.scryfall_id == CollectionEntry.scryfall_id)
        .group_by(ScryfallCard.set_code, ScryfallCard.set_name)
        .order_by(func.count(CollectionEntry.id).desc())
        .limit(limit)
    )
    return [(c, n, int(q or 0)) for c, n, q in db.execute(stmt).all()]


def distinct_languages(db: Session) -> list[str]:
    stmt = select(CollectionEntry.language).group_by(CollectionEntry.language)
    return [l for (l,) in db.execute(stmt).all() if l]


def total_collection_size(db: Session) -> int:
    return int(db.scalar(select(func.count(CollectionEntry.id))) or 0)
