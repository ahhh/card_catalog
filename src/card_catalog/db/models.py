"""All ORM models in one file. Two halves: Scryfall cache (canonical) and user collection."""

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------- Scryfall cache (owned by the sync job) ----------------------------


class ScryfallCard(Base):
    __tablename__ = "scryfall_cards"

    scryfall_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    oracle_id: Mapped[str | None] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    set_code: Mapped[str] = mapped_column(String(10), nullable=False)
    set_name: Mapped[str] = mapped_column(String, nullable=False)
    collector_number: Mapped[str] = mapped_column(String(16), nullable=False)
    rarity: Mapped[str] = mapped_column(String(16), nullable=False)
    lang: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    type_line: Mapped[str | None] = mapped_column(Text)
    oracle_text: Mapped[str | None] = mapped_column(Text)
    mana_cost: Mapped[str | None] = mapped_column(String(64))
    cmc: Mapped[float | None] = mapped_column(Float)
    colors: Mapped[str | None] = mapped_column(String(32))  # JSON array
    color_identity: Mapped[str | None] = mapped_column(String(32))  # JSON array
    finishes: Mapped[str | None] = mapped_column(String(64))  # JSON array
    image_normal_uri: Mapped[str | None] = mapped_column(Text)
    image_small_uri: Mapped[str | None] = mapped_column(Text)
    image_art_crop_uri: Mapped[str | None] = mapped_column(Text)
    card_faces_json: Mapped[str | None] = mapped_column(Text)
    rulings_uri: Mapped[str | None] = mapped_column(Text)
    tcgplayer_id: Mapped[int | None] = mapped_column(Integer, index=True)
    tcgplayer_etched_id: Mapped[int | None] = mapped_column(Integer)
    legalities_json: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())

    entries: Mapped[list["CollectionEntry"]] = relationship(back_populates="card", lazy="select")

    __table_args__ = (
        Index("ix_scryfall_set_num", "set_code", "collector_number"),
        Index("ix_scryfall_name_nocase", "name"),
    )


class CardRuling(Base):
    """Lazily-fetched Scryfall rulings, cached per scryfall_id."""

    __tablename__ = "card_rulings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scryfall_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("scryfall_cards.scryfall_id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # 'wotc', 'scryfall'
    published_at: Mapped[date | None] = mapped_column(Date)
    comment: Mapped[str] = mapped_column(Text, nullable=False)


# ---------- User collection (the part you'd send to a friend) -----------------


class CollectionEntry(Base):
    __tablename__ = "collection_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scryfall_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("scryfall_cards.scryfall_id"), nullable=False, index=True
    )
    finish: Mapped[str] = mapped_column(String(16), nullable=False, default="nonfoil")
    condition: Mapped[str] = mapped_column(String(8), nullable=False, default="NM")
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_price: Mapped[float | None] = mapped_column(Float)
    purchase_currency: Mapped[str | None] = mapped_column(String(8))
    purchase_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    for_trade: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    altered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    misprint: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), onupdate=func.now()
    )

    card: Mapped[ScryfallCard] = relationship(back_populates="entries", lazy="joined")
    tags: Mapped[list["Tag"]] = relationship(
        secondary="entry_tags", back_populates="entries", lazy="select"
    )

    __table_args__ = (
        UniqueConstraint("scryfall_id", "finish", "condition", "language", name="uq_entry_key"),
        CheckConstraint("quantity > 0", name="ck_entry_qty_pos"),
        Index("ix_entries_for_trade", "for_trade"),
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    color: Mapped[str | None] = mapped_column(String(16))  # accent color hint

    entries: Mapped[list[CollectionEntry]] = relationship(
        secondary="entry_tags", back_populates="tags"
    )


class EntryTag(Base):
    __tablename__ = "entry_tags"

    entry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("collection_entries.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )


# ---------- Pricing ------------------------------------------------------------


class PriceHistory(Base):
    __tablename__ = "price_history"

    tcgplayer_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Sub-types: "Normal", "Foil", "Foil Etched"
    sub_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, primary_key=True)
    low_price: Mapped[float | None] = mapped_column(Float)
    mid_price: Mapped[float | None] = mapped_column(Float)
    high_price: Mapped[float | None] = mapped_column(Float)
    market_price: Mapped[float | None] = mapped_column(Float)
    direct_low_price: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (Index("ix_price_recent", "tcgplayer_id", "sub_type", "as_of"),)


# ---------- Audit + config -----------------------------------------------------


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    rows_total: Mapped[int | None] = mapped_column(Integer)
    rows_imported: Mapped[int | None] = mapped_column(Integer)
    rows_skipped: Mapped[int | None] = mapped_column(Integer)
    rows_unmatched: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), onupdate=func.now()
    )
