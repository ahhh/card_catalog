"""Tests for card_catalog.domain.enums + identity helpers."""

from __future__ import annotations

import pytest

from card_catalog.domain.enums import (
    CONDITION_LABELS,
    LANGUAGE_CODES,
    Condition,
    Finish,
    Rarity,
    normalize_language,
)
from card_catalog.domain.identity import (
    CardKey,
    normalize_collector_number,
    normalize_set_code,
)


# ---- Condition.from_manabox --------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("NM", Condition.NM),
        ("nm", Condition.NM),
        ("Near Mint", Condition.NM),
        ("near_mint", Condition.NM),
        ("near-mint", Condition.NM),
        ("LP", Condition.LP),
        ("lightly played", Condition.LP),
        ("Light Play", Condition.LP),
        ("MP", Condition.MP),
        ("moderately played", Condition.MP),
        ("HP", Condition.HP),
        ("Heavily Played", Condition.HP),
        ("DMG", Condition.DMG),
        ("Damaged", Condition.DMG),
    ],
)
def test_condition_from_manabox_known(raw, expected):
    assert Condition.from_manabox(raw) is expected


@pytest.mark.parametrize("raw", ["", None, "weird thing", "BAD"])
def test_condition_from_manabox_unknown_defaults_to_nm(raw):
    assert Condition.from_manabox(raw) is Condition.NM


def test_condition_labels_cover_all_conditions():
    assert set(CONDITION_LABELS.keys()) == set(Condition)
    for c in Condition:
        assert CONDITION_LABELS[c]  # non-empty


def test_finish_values():
    assert Finish.NONFOIL.value == "nonfoil"
    assert Finish.FOIL.value == "foil"
    assert Finish.ETCHED.value == "etched"


def test_rarity_values():
    assert {r.value for r in Rarity} == {
        "common",
        "uncommon",
        "rare",
        "mythic",
        "special",
        "bonus",
    }


# ---- normalize_language ------------------------------------------------------


@pytest.mark.parametrize("name, code", list(LANGUAGE_CODES.items()))
def test_normalize_language_known(name, code):
    assert normalize_language(name) == code
    assert normalize_language(name.upper()) == code


def test_normalize_language_empty():
    assert normalize_language("") == "en"
    assert normalize_language(None) == "en"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("en", "en"),  # 2-char passes through
        ("zhs", "zhs"),  # 3-char short input
        ("EN", "en"),
        ("klingon", "kl"),  # unknown long -> first 2 chars
        ("xy", "xy"),
    ],
)
def test_normalize_language_unknown(raw, expected):
    assert normalize_language(raw) == expected


# ---- identity helpers --------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("LEA", "lea"),
        (" lea ", "lea"),
        ("", ""),
        (None, ""),
        ("Cmm", "cmm"),
    ],
)
def test_normalize_set_code(raw, expected):
    assert normalize_set_code(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("12", "12"),
        (" 12a ", "12a"),
        ("", ""),
        (None, ""),
        ("★23", "★23"),
    ],
)
def test_normalize_collector_number(raw, expected):
    assert normalize_collector_number(raw) == expected


def test_card_key_is_hashable_and_frozen():
    k1 = CardKey("sid", "nonfoil", "NM", "en")
    k2 = CardKey("sid", "nonfoil", "NM", "en")
    assert k1 == k2
    assert hash(k1) == hash(k2)
    with pytest.raises(Exception):
        # frozen=True makes attribute mutation raise FrozenInstanceError.
        k1.scryfall_id = "other"  # type: ignore[misc]
