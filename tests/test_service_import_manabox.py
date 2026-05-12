"""Tests for services.import_manabox preview + commit."""

from __future__ import annotations

import json

from card_catalog.clients.manabox_csv import ImportRow
from card_catalog.db.models import CollectionEntry, ImportRun, ScryfallCard
from card_catalog.services import import_manabox


def _seed_card(db, *, sid="sid-A", set_code="lea", number="1", tcgplayer_id=None):
    card = ScryfallCard(
        scryfall_id=sid,
        oracle_id=f"oracle-{sid}",
        name=f"Card {sid}",
        set_code=set_code,
        set_name="Test Set",
        collector_number=number,
        rarity="common",
        lang="en",
        colors=json.dumps([]),
        color_identity=json.dumps([]),
        finishes=json.dumps(["nonfoil"]),
        raw_json="{}",
        tcgplayer_id=tcgplayer_id,
    )
    db.add(card)
    db.commit()
    return card


def _row(name, set_code, num, sid=None, qty=1, finish="nonfoil"):
    return ImportRow(
        name=name,
        set_code=set_code,
        collector_number=num,
        scryfall_id=sid,
        quantity=qty,
        finish=finish,
        condition="NM",
        language="en",
    )


def test_preview_with_cache_only_resolves_inserts(db):
    _seed_card(db, sid="sid-A", set_code="lea", number="1")
    rows = [_row("Foo", "lea", "1", sid="sid-A", qty=1)]
    # Pass scryfall_client=None and ensure no live call needed (everything cached).
    preview_id, verdicts, bundle = import_manabox.preview(
        db, rows, scryfall_client=None, filename="x.csv"
    )
    assert len(verdicts) == 1
    assert verdicts[0].action == "insert"
    assert verdicts[0].scryfall_id == "sid-A"


def test_preview_increments_existing_entry(db):
    _seed_card(db, sid="sid-A")
    # Pre-existing entry with qty=2.
    db.add(
        CollectionEntry(
            scryfall_id="sid-A",
            finish="nonfoil",
            condition="NM",
            language="en",
            quantity=2,
        )
    )
    db.commit()
    rows = [_row("Foo", "lea", "1", sid="sid-A", qty=3)]
    _, verdicts, _ = import_manabox.preview(db, rows, scryfall_client=None)
    assert verdicts[0].action == "increment"
    assert verdicts[0].current_qty == 2
    assert verdicts[0].new_qty == 5


def test_preview_unmatched_row(db):
    rows = [_row("Mystery", "xx", "999", sid=None, qty=1)]
    _, verdicts, _ = import_manabox.preview(db, rows, scryfall_client=None)
    assert verdicts[0].action == "unmatched"
    assert verdicts[0].reason


def test_preview_mixed_verdicts(db):
    _seed_card(db, sid="sid-A", set_code="lea", number="1")
    _seed_card(db, sid="sid-B", set_code="lea", number="2")
    # Existing entry for sid-A.
    db.add(
        CollectionEntry(
            scryfall_id="sid-A",
            finish="nonfoil",
            condition="NM",
            language="en",
            quantity=1,
        )
    )
    db.commit()
    rows = [
        _row("A", "lea", "1", sid="sid-A", qty=1),
        _row("B", "lea", "2", sid="sid-B", qty=2),
        _row("?", "zz", "9", sid=None, qty=1),
    ]
    _, verdicts, _ = import_manabox.preview(db, rows, scryfall_client=None)
    actions = [v.action for v in verdicts]
    assert actions == ["increment", "insert", "unmatched"]


def test_preview_stash_and_commit_path(db):
    _seed_card(db, sid="sid-A", set_code="lea", number="1")
    _seed_card(db, sid="sid-B", set_code="lea", number="2")
    db.add(
        CollectionEntry(
            scryfall_id="sid-A",
            finish="nonfoil",
            condition="NM",
            language="en",
            quantity=1,
        )
    )
    db.commit()

    rows = [
        _row("A", "lea", "1", sid="sid-A", qty=2),
        _row("B", "lea", "2", sid="sid-B", qty=4),
    ]
    pid, verdicts, _ = import_manabox.preview(
        db, rows, scryfall_client=None, filename="x.csv"
    )
    # Now commit (consume removes the bundle).
    result = import_manabox.commit(db, pid, consume=True)
    assert result.inserted == 1
    assert result.incremented == 1
    assert result.rows_imported == 2
    assert result.rows_unmatched == 0

    # Check DB state.
    a = (
        db.query(CollectionEntry)
        .filter_by(scryfall_id="sid-A", finish="nonfoil")
        .one()
    )
    assert a.quantity == 3
    b = (
        db.query(CollectionEntry)
        .filter_by(scryfall_id="sid-B", finish="nonfoil")
        .one()
    )
    assert b.quantity == 4

    # ImportRun audit row written.
    runs = db.query(ImportRun).all()
    assert len(runs) == 1
    assert runs[0].rows_imported == 2
    assert runs[0].source == "manabox"
    assert runs[0].finished_at is not None


def test_commit_lookup_error_for_unknown_preview(db):
    import pytest

    with pytest.raises(LookupError):
        import_manabox.commit(db, "no-such-preview")


def test_preview_with_empty_rows_returns_empty_bundle(db):
    pid, verdicts, bundle = import_manabox.preview(db, [], scryfall_client=None)
    assert verdicts == []
    assert bundle.verdicts == []
    # Bundle is still stashed (lookup works).
    assert import_manabox.get_preview(pid) is bundle


def test_preview_falls_back_to_live_batch(db, scryfall_card_dict):
    """Cache misses are fed to client.get_collection and persisted."""

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def get_collection(self, identifiers):
            self.calls += 1
            return {"found": [scryfall_card_dict], "not_found": []}

    fake = FakeClient()
    rows = [_row("Bolt", "lea", "161", sid=None, qty=2)]
    _, verdicts, _ = import_manabox.preview(db, rows, scryfall_client=fake)
    assert fake.calls == 1
    assert verdicts[0].action == "insert"
    # Card persisted to cache by the preview step.
    persisted = db.get(ScryfallCard, scryfall_card_dict["id"])
    assert persisted is not None
