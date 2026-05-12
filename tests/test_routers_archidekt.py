"""Router tests: /archidekt/*."""

from __future__ import annotations


def test_archidekt_page_renders(client, sample_entry):
    r = client.get("/archidekt")
    assert r.status_code == 200
    # The page combines Export + Reconcile flows.
    assert "Archidekt" in r.text


def test_export_csv_has_headers_and_content(client, sample_entry):
    r = client.get("/archidekt/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "X-Skipped-Rows" in r.headers
    # First line should be the canonical header.
    assert r.text.startswith("Quantity,Scryfall ID")
    # Sample entry's scryfall id should appear on a data row.
    assert sample_entry.scryfall_id in r.text


def test_reconcile_rejects_empty_input(client):
    r = client.post("/archidekt/reconcile", data={"deck_input": ""})
    assert r.status_code == 400


def test_reconcile_rejects_bad_input(client):
    r = client.post(
        "/archidekt/reconcile", data={"deck_input": "not a deck reference"}
    )
    assert r.status_code == 400


def test_reconcile_success(client, monkeypatch, sample_card):
    """Mock the clients.archidekt.fetch_deck symbol used by the router."""
    from card_catalog.clients.archidekt import FetchedDeck, FetchedDeckCard

    fake_deck = FetchedDeck(
        id=42,
        name="Test Deck",
        owner="me",
        url="https://archidekt.com/decks/42",
        cards=[
            FetchedDeckCard(
                name="Lightning Bolt",
                oracle_id=None,
                scryfall_id=sample_card.scryfall_id,
                quantity=4,
                category="Mainboard",
            )
        ],
    )
    # The router imports fetch_deck at module load — patch where it's used.
    import card_catalog.routers.archidekt as ark_router

    monkeypatch.setattr(ark_router, "fetch_deck", lambda _x: fake_deck)
    r = client.post("/archidekt/reconcile", data={"deck_input": "42"})
    assert r.status_code == 200
    assert "Test Deck" in r.text or "Lightning Bolt" in r.text
