"""Canonical enum vocabularies. Mirror Scryfall/Manabox where possible."""

from enum import Enum


class Finish(str, Enum):
    NONFOIL = "nonfoil"
    FOIL = "foil"
    ETCHED = "etched"


class Condition(str, Enum):
    NM = "NM"
    LP = "LP"
    MP = "MP"
    HP = "HP"
    DMG = "DMG"

    @classmethod
    def from_manabox(cls, value: str) -> "Condition":
        normalized = (value or "").strip().lower().replace("_", " ").replace("-", " ")
        return {
            "near mint": cls.NM,
            "nm": cls.NM,
            "lightly played": cls.LP,
            "light play": cls.LP,
            "lp": cls.LP,
            "moderately played": cls.MP,
            "mp": cls.MP,
            "heavily played": cls.HP,
            "hp": cls.HP,
            "damaged": cls.DMG,
            "dmg": cls.DMG,
        }.get(normalized, cls.NM)


class Rarity(str, Enum):
    COMMON = "common"
    UNCOMMON = "uncommon"
    RARE = "rare"
    MYTHIC = "mythic"
    SPECIAL = "special"
    BONUS = "bonus"


CONDITION_LABELS = {
    Condition.NM: "Near Mint",
    Condition.LP: "Lightly Played",
    Condition.MP: "Moderately Played",
    Condition.HP: "Heavily Played",
    Condition.DMG: "Damaged",
}


LANGUAGE_CODES = {
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "japanese": "ja",
    "korean": "ko",
    "russian": "ru",
    "chinese simplified": "zhs",
    "simplified chinese": "zhs",
    "chinese traditional": "zht",
    "traditional chinese": "zht",
}


def normalize_language(value: str | None) -> str:
    if not value:
        return "en"
    v = value.strip().lower()
    return LANGUAGE_CODES.get(v, v[:3] if len(v) <= 3 else v[:2])


MANA_COLOR_KEYS = ("W", "U", "B", "R", "G")
