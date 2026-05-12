"""Canonical identity helpers for resolving cards across data sources."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CardKey:
    """Composite identity for a printing within the collection.

    A user can own the same printing in multiple finishes, conditions, and
    languages — each combination is a separate entry. This tuple is the
    upsert key for collection_entries.
    """

    scryfall_id: str
    finish: str
    condition: str
    language: str


def normalize_set_code(code: str | None) -> str:
    return (code or "").strip().lower()


def normalize_collector_number(number: str | None) -> str:
    # Scryfall keeps collector numbers as strings (e.g. "12a", "★23")
    return (number or "").strip()
