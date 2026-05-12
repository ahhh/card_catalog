"""Router tests: /collection, /cards/*."""

from __future__ import annotations


def test_collection_page_empty(client):
    r = client.get("/collection")
    assert r.status_code == 200
    assert "Collection" in r.text or "collection" in r.text.lower()


def test_collection_page_with_entry(client, sample_entry):
    r = client.get("/collection")
    assert r.status_code == 200
    assert "Lightning Bolt" in r.text


def test_collection_table_fragment_pushes_url(client, sample_entry):
    r = client.get(
        "/collection/table?q=bolt",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "HX-Push-Url" in r.headers or "hx-push-url" in {h.lower() for h in r.headers}


def test_collection_table_filter_text(client, sample_entry):
    r = client.get("/collection/table?q=Lightning")
    assert r.status_code == 200
    assert "Lightning Bolt" in r.text


def test_card_detail_page(client, sample_card):
    r = client.get(f"/cards/{sample_card.scryfall_id}")
    assert r.status_code == 200
    assert "Lightning Bolt" in r.text


def test_card_detail_404_for_unknown(client):
    r = client.get("/cards/no-such-card")
    assert r.status_code == 404


def test_card_detail_slideover_fragment(client, sample_card):
    r = client.get(f"/cards/{sample_card.scryfall_id}?slideover=1")
    assert r.status_code == 200


def test_entry_edit_form_fragment(client, sample_entry):
    r = client.get(f"/collection/entries/{sample_entry.id}/edit")
    assert r.status_code == 200
    # The form should reference the entry id.
    assert str(sample_entry.id) in r.text


def test_entry_patch_quantity(client, sample_entry):
    r = client.patch(
        f"/collection/entries/{sample_entry.id}",
        data={"quantity": "7"},
    )
    assert r.status_code == 200
    assert "HX-Toast" in r.headers


def test_entry_patch_quantity_zero_deletes(client, db, sample_entry):
    entry_id = sample_entry.id
    r = client.patch(
        f"/collection/entries/{entry_id}",
        data={"quantity": "0"},
    )
    assert r.status_code == 200
    assert "HX-Toast" in r.headers
    # Confirm DB state. Expire to drop stale identity-map copies.
    from card_catalog.db.models import CollectionEntry

    db.expire_all()
    assert db.get(CollectionEntry, entry_id) is None


def test_entry_patch_bad_input(client, sample_entry):
    r = client.patch(
        f"/collection/entries/{sample_entry.id}",
        data={"condition": "BAD"},
    )
    assert r.status_code == 400
    assert "HX-Toast" in r.headers


def test_entry_delete(client, db, sample_entry):
    entry_id = sample_entry.id
    r = client.delete(f"/collection/entries/{entry_id}")
    assert r.status_code == 200
    from card_catalog.db.models import CollectionEntry

    db.expire_all()
    assert db.get(CollectionEntry, entry_id) is None


def test_bulk_edit_update(client, db, sample_entry):
    r = client.post(
        "/collection/bulk-edit",
        data={"entry_ids": str(sample_entry.id), "action": "update", "for_trade": "1"},
    )
    assert r.status_code == 200
    assert "HX-Toast" in r.headers
    db.expire_all()
    from card_catalog.db.models import CollectionEntry

    assert db.get(CollectionEntry, sample_entry.id).for_trade == 1


def test_bulk_edit_delete(client, db, sample_entry):
    entry_id = sample_entry.id
    r = client.post(
        "/collection/bulk-edit",
        data={"entry_ids": str(entry_id), "action": "delete"},
    )
    assert r.status_code == 200
    from card_catalog.db.models import CollectionEntry

    db.expire_all()
    assert db.get(CollectionEntry, entry_id) is None


def test_bulk_edit_tag_requires_name(client, sample_entry):
    r = client.post(
        "/collection/bulk-edit",
        data={"entry_ids": str(sample_entry.id), "action": "tag", "tag_name": ""},
    )
    assert r.status_code == 400
