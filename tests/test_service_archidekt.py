"""Tests for services.archidekt."""

from __future__ import annotations

import json
from datetime import date

import pytest

from card_catalog.clients.archidekt import FetchedDeck, FetchedDeckCard
from card_catalog.db.models import CollectionEntry, PriceHistory, ScryfallCard
from card_catalog.services import archidekt as svc


def _seed_card(db, *, sid, oracle, set_code="lea", number="1", tcgplayer_id=None):
    c = ScryfallCard(
        scryfall_id=sid,
        oracle_id=oracle,
        name=f"Card {sid}",
        set_code=set_code,
        set_name="Set",
        collector_number=number,
        rarity="common",
        lang="en",
        colors=json.dumps([]),
        color_identity=json.dumps([]),
        finishes=json.dumps(["nonfoil"]),
        raw_json="{}",
        tcgplayer_id=tcgplayer_id,
        image_small_uri=f"https://example.com/{sid}.jpg",
    )
    db.add(c)
    db.commit()
    return c


def _seed_entry(db, card, qty=1, finish="nonfoil"):
    e = CollectionEntry(
        scryfall_id=card.scryfall_id,
        finish=finish,
        condition="NM",
        language="en",
        quantity=qty,
    )
    db.add(e)
    db.commit()
    return e


# ---- export_filtered_collection -------------------------------------------


def test_export_filtered_collection_aggregates(db):
    c1 = _seed_card(db, sid="sid-1", oracle="o1")
    c2 = _seed_card(db, sid="sid-2", oracle="o2")
    _seed_entry(db, c1, qty=2, finish="nonfoil")
    _seed_entry(db, c1, qty=1, finish="foil")  # different finish, same sid
    _seed_entry(db, c2, qty=3)
    body, filename = svc.export_filtered_collection(db)
    lines = body.strip().split("\n")
    assert lines[0] == "Quantity,Scryfall ID"
    # Should aggregate sid-1 to 3.
    rows = {line.split(",")[1]: int(line.split(",")[0]) for line in lines[1:]}
    assert rows["sid-1"] == 3
    assert rows["sid-2"] == 3
    assert filename.startswith("card-catalog-export-")
    assert filename.endswith(".csv")


def test_export_filtered_collection_empty(db):
    body, filename = svc.export_filtered_collection(db)
    lines = body.strip().split("\n")
    assert lines == ["Quantity,Scryfall ID"]


# ---- reconcile_with_deck --------------------------------------------------


def _deck_card(quantity=1, scryfall_id=None, name="Test"):
    return FetchedDeckCard(
        name=name,
        oracle_id=None,
        scryfall_id=scryfall_id,
        quantity=quantity,
        category="Mainboard",
    )


def _deck(*cards):
    return FetchedDeck(id=1, name="Test", owner=None, url="x", cards=list(cards))


def test_reconcile_owned_via_oracle_id(db):
    # Two printings of the same oracle. Own one, deck wants the other.
    c1 = _seed_card(db, sid="sid-A", oracle="oracle-X", set_code="lea")
    _seed_card(db, sid="sid-B", oracle="oracle-X", set_code="leb")
    _seed_entry(db, c1, qty=2)

    deck = _deck(_deck_card(quantity=3, scryfall_id="sid-B", name="Card"))
    report = svc.reconcile_with_deck(db, deck)

    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.needed_qty == 3
    assert row.owned_qty == 2  # via oracle match
    assert row.missing_qty == 1


def test_reconcile_no_match_all_missing(db):
    deck = _deck(_deck_card(quantity=2, scryfall_id="unknown-sid", name="Mystery"))
    report = svc.reconcile_with_deck(db, deck)
    row = report.rows[0]
    assert row.owned_qty == 0
    assert row.missing_qty == 2
    assert report.total_needed == 2
    assert report.total_missing == 2


def test_reconcile_owned_caps_at_needed(db):
    c1 = _seed_card(db, sid="sid-A", oracle="oracle-A")
    _seed_entry(db, c1, qty=10)  # We have more than we need.
    deck = _deck(_deck_card(quantity=2, scryfall_id="sid-A"))
    report = svc.reconcile_with_deck(db, deck)
    assert report.total_owned == 2  # capped
    assert report.total_missing == 0


def test_reconcile_est_completion_cost(db):
    _seed_card(db, sid="sid-A", oracle="o1", tcgplayer_id=111)
    _seed_card(db, sid="sid-B", oracle="o2", tcgplayer_id=222, set_code="leb")
    # No copies owned.
    db.add(
        PriceHistory(
            tcgplayer_id=111,
            sub_type="Normal",
            as_of=date.today(),
            market_price=5.0,
        )
    )
    db.add(
        PriceHistory(
            tcgplayer_id=222,
            sub_type="Normal",
            as_of=date.today(),
            market_price=2.0,
            mid_price=2.0,
        )
    )
    db.commit()

    deck = _deck(
        _deck_card(quantity=2, scryfall_id="sid-A", name="A"),
        _deck_card(quantity=1, scryfall_id="sid-B", name="B"),
    )
    report = svc.reconcile_with_deck(db, deck)
    # 2 of sid-A at $5 + 1 of sid-B at $2 = $12
    assert report.est_completion_cost == pytest.approx(12.0)


def test_reconcile_est_completion_none_when_no_prices(db):
    _seed_card(db, sid="sid-A", oracle="o1")
    deck = _deck(_deck_card(quantity=1, scryfall_id="sid-A"))
    report = svc.reconcile_with_deck(db, deck)
    assert report.est_completion_cost is None


def test_reconcile_skips_zero_quantity_cards(db):
    _seed_card(db, sid="sid-A", oracle="o1")
    deck = _deck(
        _deck_card(quantity=0, scryfall_id="sid-A"),
        _deck_card(quantity=2, scryfall_id="sid-A", name="A"),
    )
    report = svc.reconcile_with_deck(db, deck)
    assert len(report.rows) == 1
    assert report.rows[0].needed_qty == 2


def test_reconcile_row_image_picks_owned_printing_image(db):
    c1 = _seed_card(db, sid="sid-A", oracle="o1")
    _seed_entry(db, c1, qty=1)
    deck = _deck(_deck_card(quantity=2, scryfall_id="sid-A"))
    report = svc.reconcile_with_deck(db, deck)
    assert "sid-A.jpg" in (report.rows[0].image_uri or "")
