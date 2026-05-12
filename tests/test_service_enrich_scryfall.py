"""Tests for services.enrich_scryfall."""

from __future__ import annotations

import json

from card_catalog.db.models import ScryfallCard
from card_catalog.services import enrich_scryfall


def test_upsert_card_dict_creates_row(db, scryfall_card_dict):
    out = enrich_scryfall.upsert_card_dict(db, scryfall_card_dict)
    assert out is not None
    assert out.scryfall_id == scryfall_card_dict["id"]
    assert out.set_code == "lea"  # lowercased
    # JSON arrays were serialized.
    assert json.loads(out.colors) == ["R"]
    assert json.loads(out.color_identity) == ["R"]
    assert json.loads(out.finishes) == ["nonfoil"]
    # Image URIs picked from image_uris.
    assert out.image_small_uri.endswith("small/abc.jpg")
    assert out.image_normal_uri.endswith("normal/abc.jpg")


def test_upsert_is_idempotent(db, scryfall_card_dict):
    enrich_scryfall.upsert_card_dict(db, scryfall_card_dict)
    # Mutate a field and re-upsert.
    scryfall_card_dict["name"] = "Lightning Bolt (reprint)"
    enrich_scryfall.upsert_card_dict(db, scryfall_card_dict)
    rows = db.query(ScryfallCard).filter_by(scryfall_id=scryfall_card_dict["id"]).all()
    assert len(rows) == 1
    assert rows[0].name == "Lightning Bolt (reprint)"


def test_upsert_card_dicts_bulk(db, scryfall_card_dict):
    others = []
    for i in range(3):
        d = dict(scryfall_card_dict)
        d["id"] = f"sid-bulk-{i}"
        d["name"] = f"Card {i}"
        others.append(d)
    n = enrich_scryfall.upsert_card_dicts(db, others)
    assert n == 3
    assert db.query(ScryfallCard).count() == 3


def test_upsert_card_dict_returns_none_on_missing_id(db):
    assert enrich_scryfall.upsert_card_dict(db, {}) is None
    assert enrich_scryfall.upsert_card_dict(db, {"name": "x"}) is None


def test_last_sync_stats_shape(db, scryfall_card_dict):
    stats = enrich_scryfall.last_sync_stats(db)
    assert stats == {"card_count": 0, "last_fetched_at": None}
    enrich_scryfall.upsert_card_dict(db, scryfall_card_dict)
    stats = enrich_scryfall.last_sync_stats(db)
    assert stats["card_count"] == 1
    assert stats["last_fetched_at"] is not None


def test_card_faces_image_fallback(db):
    """DFCs have image_uris under faces, not on the card root."""
    card = {
        "id": "dfc-1",
        "name": "Delver/Insectile",
        "set": "isd",
        "set_name": "Innistrad",
        "collector_number": "51",
        "rarity": "common",
        "lang": "en",
        "card_faces": [
            {
                "name": "Delver of Secrets",
                "image_uris": {
                    "small": "small://face.jpg",
                    "normal": "normal://face.jpg",
                    "art_crop": "art_crop://face.jpg",
                },
            }
        ],
    }
    enrich_scryfall.upsert_card_dict(db, card)
    persisted = db.get(ScryfallCard, "dfc-1")
    assert persisted.image_small_uri == "small://face.jpg"
    assert persisted.image_normal_uri == "normal://face.jpg"


def test_lookup_or_fetch_returns_cached(db, scryfall_card_dict):
    enrich_scryfall.upsert_card_dict(db, scryfall_card_dict)
    out = enrich_scryfall.lookup_or_fetch(db, "lea", "161", lang="en")
    assert out is not None
    assert out.scryfall_id == scryfall_card_dict["id"]


def test_lookup_or_fetch_falls_back_to_client(db, scryfall_card_dict):
    class FakeClient:
        def get_by_set_number(self, set_code, number, lang="en"):
            return scryfall_card_dict

    out = enrich_scryfall.lookup_or_fetch(
        db, "lea", "161", lang="en", client=FakeClient()
    )
    assert out is not None
    # Persisted.
    assert db.get(ScryfallCard, scryfall_card_dict["id"]) is not None
