"""Tests for clients.manabox_csv (header tolerance + row normalization)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from card_catalog.clients.manabox_csv import ImportRow, parse


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_sample_csv_basic_fields():
    rows, warnings = parse((FIXTURES / "manabox_sample.csv").read_bytes())
    # The fixture has 6 importable rows.
    assert len(rows) == 6

    # First row spot check.
    r = rows[0]
    assert isinstance(r, ImportRow)
    assert r.name == "Sol Ring"
    assert r.set_code == "cmm"  # lowercased
    assert r.collector_number == "410"
    assert r.scryfall_id == "9a3d0e8c-1b3f-4f9e-8a2b-1c8e0d5b7421"
    assert r.quantity == 1
    assert r.finish == "nonfoil"  # 'normal' -> 'nonfoil'
    assert r.condition == "NM"  # "Near Mint" -> NM
    assert r.language == "en"
    assert r.purchase_price == 1.25
    assert r.purchase_currency == "USD"


def test_parse_finish_variants():
    """Foil/etched/normal column mapped to canonical enum value."""
    rows, _ = parse((FIXTURES / "manabox_sample.csv").read_bytes())
    finishes = {r.name: r.finish for r in rows}
    assert finishes["Cyclonic Rift"] == "foil"
    assert finishes["Mana Drain"] == "etched"


def test_parse_language_mapping():
    rows, _ = parse((FIXTURES / "manabox_sample.csv").read_bytes())
    langs = {r.name: r.language for r in rows}
    assert langs["Mana Drain"] == "ja"
    assert langs["Sol Ring"] == "en"


def test_parse_condition_mapping():
    rows, _ = parse((FIXTURES / "manabox_sample.csv").read_bytes())
    conds = {r.name: r.condition for r in rows if r.name}
    assert conds["Dockside Extortionist"] == "LP"


def test_parse_drops_rows_without_identifiers():
    csv = (
        "Name,Quantity\n"
        "Mystery Card,1\n"
        "Other Mystery,2\n"
    )
    rows, warnings = parse(csv)
    assert rows == []
    # The parser short-circuits when neither identifier column is present.
    assert any("missing identifier columns" in w.lower() or "identifier" in w.lower() for w in warnings)


def test_parse_drops_individual_row_with_no_identifiers():
    csv = (
        "Name,Set code,Collector number,Scryfall ID,Quantity\n"
        ",,,,,3\n"  # truly empty
        "Good Card,lea,1,,1\n"
    )
    rows, warnings = parse(csv)
    # The bad row should be skipped, the good one should remain.
    assert len(rows) == 1
    assert any("skipped" in w.lower() for w in warnings)


def test_parse_card_number_alias():
    """`Card number` is an alias for collector_number."""
    csv = (
        "Name,Set code,Card number,Quantity\n"
        "Sol Ring,cmm,410,1\n"
    )
    rows, _ = parse(csv)
    assert len(rows) == 1
    assert rows[0].collector_number == "410"


def test_parse_mixed_case_headers():
    csv = (
        "name,SET CODE,Collector Number,QUANTITY\n"
        "Sol Ring,cmm,410,3\n"
    )
    rows, warnings = parse(csv)
    assert len(rows) == 1
    assert rows[0].quantity == 3
    assert rows[0].set_code == "cmm"


def test_parse_unknown_column_warns_but_keeps_row():
    csv = (
        "Name,Set code,Collector number,Quantity,Vendor Tag\n"
        "Sol Ring,cmm,410,1,EBay\n"
    )
    rows, warnings = parse(csv)
    assert len(rows) == 1
    assert any("Vendor Tag" in w or "vendor tag" in w.lower() for w in warnings)


def test_parse_quantity_defaults_to_one():
    csv = (
        "Name,Set code,Collector number\n"
        "Sol Ring,cmm,410\n"
    )
    rows, warnings = parse(csv)
    assert len(rows) == 1
    assert rows[0].quantity == 1
    assert any("Quantity" in w for w in warnings)


def test_parse_empty_csv():
    rows, warnings = parse("")
    assert rows == []
    assert warnings  # non-empty


def test_parse_accepts_string_and_bytes_and_file():
    text = "Name,Set code,Collector number,Quantity\nSol Ring,cmm,410,1\n"
    r1, _ = parse(text)
    r2, _ = parse(text.encode("utf-8"))
    r3, _ = parse(io.StringIO(text))
    r4, _ = parse(io.BytesIO(text.encode("utf-8")))
    for rows in (r1, r2, r3, r4):
        assert len(rows) == 1
        assert rows[0].name == "Sol Ring"


def test_parse_handles_utf8_bom():
    text = "Name,Set code,Collector number,Quantity\nSol Ring,cmm,410,1\n"
    # Encode with utf-8-sig so a BOM is prepended.
    rows, _ = parse(text.encode("utf-8-sig"))
    assert len(rows) == 1
    assert rows[0].name == "Sol Ring"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1.25", 1.25),
        ("$1.50", 1.50),
        ("€2.00", 2.0),
        ("", None),
        ("abc", None),
    ],
)
def test_purchase_price_coercion(raw, expected):
    csv = (
        "Name,Set code,Collector number,Quantity,Purchase price\n"
        f"Sol Ring,cmm,410,1,{raw}\n"
    )
    rows, _ = parse(csv)
    assert rows[0].purchase_price == expected


def test_purchase_price_coercion_with_comma_thousands():
    """Comma-as-thousands-separator with the value quoted in the CSV row."""
    csv = (
        "Name,Set code,Collector number,Quantity,Purchase price\n"
        'Sol Ring,cmm,410,1,"1,234.5"\n'
    )
    rows, _ = parse(csv)
    assert rows[0].purchase_price == 1234.5


def test_purchase_currency_uppercased():
    csv = (
        "Name,Set code,Collector number,Quantity,Purchase currency\n"
        "Sol Ring,cmm,410,1,usd\n"
    )
    rows, _ = parse(csv)
    assert rows[0].purchase_currency == "USD"


def test_altered_and_misprint_bools():
    csv = (
        "Name,Set code,Collector number,Quantity,Altered,Misprint\n"
        "Sol Ring,cmm,410,1,true,1\n"
        "Sol Ring,cmm,411,1,no,false\n"
    )
    rows, _ = parse(csv)
    assert rows[0].altered is True
    assert rows[0].misprint is True
    assert rows[1].altered is False
    assert rows[1].misprint is False


def test_import_row_lookup_key():
    r = ImportRow(
        name="x",
        set_code="lea",
        collector_number="42",
        scryfall_id=None,
        quantity=1,
        finish="nonfoil",
        condition="NM",
        language="en",
    )
    assert r.lookup_key == ("lea", "42")
