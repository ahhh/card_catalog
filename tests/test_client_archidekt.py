"""Tests for clients.archidekt (CSV export, deck fetch wrapping)."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from card_catalog.clients import archidekt as ark
from card_catalog.clients.archidekt import (
    ArchidektError,
    export_to_csv,
    fetch_deck,
    parse_deck_id,
)


# ---- parse_deck_id ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (12345, 12345),
        ("12345", 12345),
        ("https://archidekt.com/decks/77/foo", 77),
        ("http://www.archidekt.com/decks/9", 9),
        ("archidekt.com/decks/42/the-deck/", 42),
        ("https://ARCHIDEKT.COM/decks/3/something?x=1", 3),
    ],
)
def test_parse_deck_id_valid(raw, expected):
    assert parse_deck_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "nope", "https://example.com/decks/123", -1, 0],
)
def test_parse_deck_id_invalid(raw):
    with pytest.raises(ArchidektError):
        parse_deck_id(raw)


def test_parse_deck_id_wrong_type():
    with pytest.raises(ArchidektError):
        parse_deck_id(3.14)  # type: ignore[arg-type]


# ---- export_to_csv ----------------------------------------------------------


def test_export_to_csv_writes_header_and_rows():
    buf = io.StringIO()
    n = export_to_csv(
        [(2, "sid-a"), (1, "sid-b"), (4, "sid-c")],
        buf,
    )
    text = buf.getvalue()
    assert n == 3
    lines = text.strip().split("\n")
    assert lines[0] == "Quantity,Scryfall ID"
    assert lines[1] == "2,sid-a"
    assert lines[2] == "1,sid-b"
    assert lines[3] == "4,sid-c"


def test_export_to_csv_skips_blank_scryfall_id():
    buf = io.StringIO()
    n = export_to_csv([(1, "sid-a"), (3, ""), (2, "sid-b")], buf)
    assert n == 2
    body = buf.getvalue()
    assert "sid-a" in body and "sid-b" in body
    # Empty SID row dropped.
    assert body.count("\n") == 3  # header + 2 data + trailing newline


# ---- fetch_deck (with mocked pyrchidekt) -----------------------------------


def _make_fake_deck():
    oracle = SimpleNamespace(
        name="Lightning Bolt",
        cmc=1,
        mana_cost="{R}",
        colors=["Red"],
        types=["Instant"],
        sub_types=[],
        super_types=[],
    )
    inner = SimpleNamespace(uid="sid-bolt", oracle_card=oracle)
    arch_card = SimpleNamespace(
        quantity=4,
        categories=["Mainboard"],
        card=inner,
    )
    return SimpleNamespace(
        id=42,
        name="Burn",
        owner=SimpleNamespace(username="someone"),
        cards=[arch_card],
        categories=[],
    )


def test_fetch_deck_normalizes_pyrchidekt_response(monkeypatch):
    # We monkeypatch the symbol at import time inside the function.
    import sys
    fake_mod = SimpleNamespace(getDeckById=lambda _id: _make_fake_deck())
    fake_pkg = SimpleNamespace(api=fake_mod)
    monkeypatch.setitem(sys.modules, "pyrchidekt", fake_pkg)
    monkeypatch.setitem(sys.modules, "pyrchidekt.api", fake_mod)

    deck = fetch_deck(42)
    assert deck.id == 42
    assert deck.name == "Burn"
    assert deck.owner == "someone"
    assert len(deck.cards) == 1
    c = deck.cards[0]
    assert c.name == "Lightning Bolt"
    assert c.scryfall_id == "sid-bolt"
    assert c.quantity == 4
    assert c.colors == ["R"]
    assert c.cmc == 1.0
    assert c.category == "Mainboard"


def test_fetch_deck_runtime_error_404_mapped(monkeypatch):
    import sys

    def _raise(_id):
        raise RuntimeError("not a valid deck id")

    fake_mod = SimpleNamespace(getDeckById=_raise)
    monkeypatch.setitem(sys.modules, "pyrchidekt", SimpleNamespace(api=fake_mod))
    monkeypatch.setitem(sys.modules, "pyrchidekt.api", fake_mod)

    with pytest.raises(ArchidektError) as info:
        fetch_deck(99)
    assert "private" in str(info.value) or "could not be found" in str(info.value)


def test_fetch_deck_generic_runtime_error(monkeypatch):
    import sys

    def _raise(_id):
        raise RuntimeError("something else")

    fake_mod = SimpleNamespace(getDeckById=_raise)
    monkeypatch.setitem(sys.modules, "pyrchidekt", SimpleNamespace(api=fake_mod))
    monkeypatch.setitem(sys.modules, "pyrchidekt.api", fake_mod)

    with pytest.raises(ArchidektError):
        fetch_deck(99)


def test_fetch_deck_other_exception_wrapped(monkeypatch):
    import sys

    class FakeNetErr(Exception):
        pass

    def _raise(_id):
        raise FakeNetErr("ECONNREFUSED")

    fake_mod = SimpleNamespace(getDeckById=_raise)
    monkeypatch.setitem(sys.modules, "pyrchidekt", SimpleNamespace(api=fake_mod))
    monkeypatch.setitem(sys.modules, "pyrchidekt.api", fake_mod)

    with pytest.raises(ArchidektError) as info:
        fetch_deck(99)
    assert "internet" in str(info.value).lower() or "FakeNetErr" in str(info.value)


# ---- color normalization ----------------------------------------------------


def test_normalize_colors_handles_long_names():
    out = ark._normalize_colors(["White", "Blue"])
    assert out == ["W", "U"]


def test_normalize_colors_dedupes():
    out = ark._normalize_colors(["W", "white"])
    assert out == ["W"]


def test_normalize_colors_skips_non_string():
    assert ark._normalize_colors([None, 1, "Red"]) == ["R"]
    assert ark._normalize_colors(None) == []


def test_pick_category_prefers_commander():
    inner = SimpleNamespace(card=SimpleNamespace(oracle_card=None))
    inner.categories = ["Mainboard", "Commander"]
    assert ark._pick_category(inner) == "Commander"


def test_pick_category_falls_back_to_mainboard():
    inner = SimpleNamespace(card=SimpleNamespace(oracle_card=None), categories=[])
    assert ark._pick_category(inner) == "Mainboard"


def test_build_type_line_with_subtypes():
    oc = SimpleNamespace(
        super_types=["Legendary"],
        types=["Creature"],
        sub_types=["Human", "Wizard"],
    )
    assert ark._build_type_line(oc) == "Legendary Creature — Human Wizard"


def test_build_type_line_none_for_empty():
    oc = SimpleNamespace(super_types=[], types=[], sub_types=[])
    assert ark._build_type_line(oc) is None
    assert ark._build_type_line(None) is None
