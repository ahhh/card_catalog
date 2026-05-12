"""Tests for services.prices."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from card_catalog.db.models import (
    CollectionEntry,
    PriceHistory,
    ScryfallCard,
)
from card_catalog.jobs.registry import JobStatus, registry
from card_catalog.services import prices as prices_svc


def _seed_card(db, *, sid="card-1", tcgplayer_id=100, etched_id=None, set_code="lea"):
    c = ScryfallCard(
        scryfall_id=sid,
        oracle_id=f"oracle-{sid}",
        name="Test",
        set_code=set_code,
        set_name="Test Set",
        collector_number="1",
        rarity="common",
        lang="en",
        colors=json.dumps([]),
        color_identity=json.dumps([]),
        finishes=json.dumps(["nonfoil"]),
        raw_json="{}",
        tcgplayer_id=tcgplayer_id,
        tcgplayer_etched_id=etched_id,
    )
    db.add(c)
    db.commit()
    return c


def _seed_entry(db, card, finish="nonfoil", qty=1):
    e = CollectionEntry(
        scryfall_id=card.scryfall_id,
        finish=finish,
        condition="NM",
        language="en",
        quantity=qty,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def test_latest_price_for_entry_prefers_market(db):
    card = _seed_card(db, tcgplayer_id=500)
    entry = _seed_entry(db, card)
    db.add(
        PriceHistory(
            tcgplayer_id=500,
            sub_type="Normal",
            as_of=date.today(),
            market_price=2.5,
            mid_price=1.0,
        )
    )
    db.commit()
    assert prices_svc.latest_price_for_entry(db, entry) == 2.5


def test_latest_price_for_entry_falls_back_to_mid(db):
    card = _seed_card(db, tcgplayer_id=501)
    entry = _seed_entry(db, card)
    db.add(
        PriceHistory(
            tcgplayer_id=501,
            sub_type="Normal",
            as_of=date.today(),
            market_price=None,
            mid_price=0.75,
        )
    )
    db.commit()
    assert prices_svc.latest_price_for_entry(db, entry) == 0.75


def test_latest_price_for_entry_returns_none_when_no_history(db):
    card = _seed_card(db, tcgplayer_id=502)
    entry = _seed_entry(db, card)
    assert prices_svc.latest_price_for_entry(db, entry) is None


def test_latest_price_for_entry_returns_none_without_tcg_id(db):
    card = _seed_card(db, tcgplayer_id=None)
    entry = _seed_entry(db, card)
    assert prices_svc.latest_price_for_entry(db, entry) is None


def test_history_points_ordered_oldest_first(db):
    _seed_card(db, tcgplayer_id=600)
    today = date.today()
    for n, price in zip([3, 1, 2], [3.0, 1.0, 2.0]):
        db.add(
            PriceHistory(
                tcgplayer_id=600,
                sub_type="Normal",
                as_of=today - timedelta(days=n),
                market_price=price,
            )
        )
    db.commit()
    pts = prices_svc.history_points(db, 600, "Normal", days=30)
    # Stored as_of order: (today-3, 3.0), (today-2, 2.0), (today-1, 1.0)
    # Service returns oldest -> newest by date.
    assert pts == [3.0, 2.0, 1.0]


def test_history_points_returns_empty_with_no_tcg(db):
    assert prices_svc.history_points(db, 0, "Normal") == []


def test_collection_value_estimate(db):
    card = _seed_card(db, tcgplayer_id=700)
    _seed_entry(db, card, qty=3)
    db.add(
        PriceHistory(
            tcgplayer_id=700,
            sub_type="Normal",
            as_of=date.today(),
            market_price=4.0,
            mid_price=3.5,
        )
    )
    db.commit()
    total = prices_svc.collection_value_estimate(db)
    assert total == pytest.approx(12.0)


def test_tracked_tcgplayer_count(db):
    c1 = _seed_card(db, sid="x1", tcgplayer_id=1)
    c2 = _seed_card(db, sid="x2", tcgplayer_id=2)
    c3 = _seed_card(db, sid="x3", tcgplayer_id=None)
    _seed_entry(db, c1)
    _seed_entry(db, c2)
    _seed_entry(db, c3)
    assert prices_svc.tracked_tcgplayer_count(db) == 2


def test_latest_refresh_at(db):
    assert prices_svc.latest_refresh_at(db) is None
    db.add(
        PriceHistory(
            tcgplayer_id=1, sub_type="Normal", as_of=date(2026, 5, 12), market_price=1.0
        )
    )
    db.commit()
    out = prices_svc.latest_refresh_at(db)
    assert out is not None
    assert out.year == 2026 and out.month == 5 and out.day == 12


# ---- refresh_collection_prices end-to-end with stub client ---------------


class _FakeTCGCSV:
    def __init__(self, *, groups, prices_by_group):
        self.groups = groups
        self.prices_by_group = prices_by_group

    def list_groups(self):
        return list(self.groups)

    def get_prices(self, group_id):
        return list(self.prices_by_group.get(group_id, []))


def test_refresh_collection_prices_empty_collection(db):
    job = registry.create("prices_refresh", "label")
    fake = _FakeTCGCSV(groups=[], prices_by_group={})
    prices_svc.refresh_collection_prices(db, job, fake)
    assert job.status == JobStatus.COMPLETED
    assert job.extras["updates"] == 0


def test_refresh_collection_prices_inserts_rows(db, monkeypatch):
    card = _seed_card(db, tcgplayer_id=42, set_code="lea")
    _seed_entry(db, card)
    job = registry.create("prices_refresh", "label")
    fake = _FakeTCGCSV(
        groups=[
            {
                "groupId": 7,
                "name": "Limited Edition Alpha",
                "abbreviation": "LEA",
                "publishedOn": "1993-08-05",
            }
        ],
        prices_by_group={
            7: [
                {
                    "productId": 42,
                    "subTypeName": "Normal",
                    "lowPrice": 1.0,
                    "midPrice": 2.0,
                    "highPrice": 3.0,
                    "marketPrice": 2.5,
                    "directLowPrice": 1.5,
                }
            ]
        },
    )
    # Avoid stale module cache from a previous test.
    monkeypatch.setattr(prices_svc, "_groups_cache", None)
    prices_svc.refresh_collection_prices(db, job, fake)
    assert job.status == JobStatus.COMPLETED
    assert job.extras["mapped_sets"] == 1
    assert job.extras["updates"] == 1
    row = db.query(PriceHistory).filter_by(tcgplayer_id=42, sub_type="Normal").one()
    assert row.market_price == 2.5


def test_refresh_collection_prices_unmapped_set_recorded(db, monkeypatch):
    card = _seed_card(db, tcgplayer_id=99, set_code="weird")
    _seed_entry(db, card)
    job = registry.create("prices_refresh", "label")
    fake = _FakeTCGCSV(
        groups=[
            {
                "groupId": 1,
                "name": "Other",
                "abbreviation": "OTH",
                "publishedOn": "2024",
            }
        ],
        prices_by_group={},
    )
    monkeypatch.setattr(prices_svc, "_groups_cache", None)
    prices_svc.refresh_collection_prices(db, job, fake)
    assert "weird" in job.extras["unmapped_sets"]
    assert job.status == JobStatus.COMPLETED
