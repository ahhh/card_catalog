"""Smoke tests for the SQLAlchemy models and their constraints."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from card_catalog.db.models import (
    CardRuling,
    CollectionEntry,
    EntryTag,
    ImportRun,
    PriceHistory,
    ScryfallCard,
    Setting,
    Tag,
)


def test_scryfall_card_round_trip(db, sample_card):
    fetched = db.get(ScryfallCard, sample_card.scryfall_id)
    assert fetched is not None
    assert fetched.name == "Lightning Bolt"
    assert fetched.set_code == "lea"


def test_collection_entry_round_trip(db, sample_entry, sample_card):
    e = db.get(CollectionEntry, sample_entry.id)
    assert e.scryfall_id == sample_card.scryfall_id
    assert e.quantity == 2


def test_collection_entry_unique_constraint(db, sample_card):
    e1 = CollectionEntry(
        scryfall_id=sample_card.scryfall_id,
        finish="nonfoil",
        condition="NM",
        language="en",
        quantity=1,
    )
    db.add(e1)
    db.commit()
    e2 = CollectionEntry(
        scryfall_id=sample_card.scryfall_id,
        finish="nonfoil",
        condition="NM",
        language="en",
        quantity=1,
    )
    db.add(e2)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_collection_entry_quantity_check(db, sample_card):
    bad = CollectionEntry(
        scryfall_id=sample_card.scryfall_id,
        finish="nonfoil",
        condition="NM",
        language="en",
        quantity=0,
    )
    db.add(bad)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_entry_tag_cascades_when_entry_deleted(db, sample_entry):
    tag = Tag(name="favorites")
    db.add(tag)
    db.commit()
    db.add(EntryTag(entry_id=sample_entry.id, tag_id=tag.id))
    db.commit()
    assert db.scalar(select(EntryTag).where(EntryTag.entry_id == sample_entry.id))

    db.delete(sample_entry)
    db.commit()
    assert (
        db.scalar(select(EntryTag).where(EntryTag.entry_id == sample_entry.id))
        is None
    )


def test_collection_entry_relationship_loads_card(db, sample_entry, sample_card):
    e = db.get(CollectionEntry, sample_entry.id)
    assert e.card is not None
    assert e.card.scryfall_id == sample_card.scryfall_id


def test_price_history_round_trip(db):
    db.add(
        PriceHistory(
            tcgplayer_id=1,
            sub_type="Normal",
            as_of=date(2026, 1, 1),
            market_price=1.5,
            mid_price=1.4,
        )
    )
    db.commit()
    rows = db.execute(select(PriceHistory)).scalars().all()
    assert len(rows) == 1
    assert rows[0].market_price == 1.5


def test_import_run_round_trip(db):
    run = ImportRun(source="manabox", filename="x.csv", rows_total=10)
    db.add(run)
    db.commit()
    assert run.id is not None


def test_setting_round_trip(db):
    s = Setting(key="hello", value="world")
    db.add(s)
    db.commit()
    fetched = db.get(Setting, "hello")
    assert fetched.value == "world"


def test_card_ruling_round_trip(db, sample_card):
    rule = CardRuling(
        scryfall_id=sample_card.scryfall_id,
        source="wotc",
        published_at=date(2024, 1, 1),
        comment="bolt is direct damage",
    )
    db.add(rule)
    db.commit()
    assert rule.id is not None
