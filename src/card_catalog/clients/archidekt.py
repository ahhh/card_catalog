"""Archidekt client: CSV export (push) + pyrchidekt deck fetch (pull).

This module is the one-file isolation layer for everything Archidekt-shaped.
Two responsibilities:

1. ``export_to_csv`` writes a CSV in the minimum format Archidekt's manual web
   importer documents: a ``Quantity,Scryfall ID`` header followed by one row
   per stack. The user downloads the file then uploads it via archidekt.com.

2. ``fetch_deck`` is a thin wrapper around the third-party ``pyrchidekt``
   library that returns a *normalized* dataclass so the rest of the codebase
   never touches pyrchidekt's surface directly.

----------------------------------------------------------------------------
pyrchidekt 2.2.0 API used (verified against
.venv/lib/python3.14/site-packages/pyrchidekt/):

    from pyrchidekt.api import getDeckById         # int id -> Deck

    Deck:
        .id            int
        .name          str
        .owner         Owner (with .username)
        .cards         list[ArchidektCard]   (flat list)
        .categories    list[Category]        (each with .cards)

    ArchidektCard:
        .quantity      int
        .categories    list[str]             (e.g. ["Commander"], ["Mainboard"])
        .card          Card

    Card:
        .uid           str  -- the Scryfall UUID for the printing
        .oracle_card   OracleCard

    OracleCard:
        .name          str
        .cmc           int  (may be -1 if missing)
        .mana_cost     str
        .colors        list[str]   (full color names like "White", "Blue")
        .types         list[str]
        .sub_types     list[str]
        .super_types   list[str]

Note: pyrchidekt does NOT expose an oracle_id directly. We pass through the
Scryfall printing ``uid`` so the service layer can look up our local
ScryfallCard row and read ``oracle_id`` from there. If pyrchidekt's surface
shifts in a future release, this is the only file that should need updates.

``getDeckById`` raises ``RuntimeError`` on 404/network failures; we re-raise
as ``ArchidektError`` so callers can ``except`` cleanly.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from typing import Iterable, TextIO


# ---- CSV export ------------------------------------------------------------


CSV_HEADER: tuple[str, str] = ("Quantity", "Scryfall ID")


def export_to_csv(rows: Iterable[tuple[int, str]], buf: TextIO) -> int:
    """Write ``Quantity,Scryfall ID`` CSV to ``buf`` and return the row count.

    Rows are written exactly as supplied; the caller is responsible for
    aggregation (services/archidekt.py does it via SQL GROUP BY).

    Pure stdlib — no pandas dependency. Archidekt's web importer documents
    this two-column form as the minimum it round-trips reliably.
    """
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    count = 0
    for qty, scryfall_id in rows:
        if not scryfall_id:
            continue
        writer.writerow([int(qty), scryfall_id])
        count += 1
    return count


# ---- Normalized fetch result -----------------------------------------------


@dataclass
class FetchedDeckCard:
    name: str
    oracle_id: str | None
    scryfall_id: str | None
    quantity: int
    category: str  # "Commander" | "Mainboard" | "Sideboard" | etc.
    cmc: float | None = None
    mana_cost: str | None = None
    type_line: str | None = None
    colors: list[str] = field(default_factory=list)


@dataclass
class FetchedDeck:
    id: int
    name: str
    owner: str | None
    url: str
    cards: list[FetchedDeckCard]


class ArchidektError(Exception):
    """Raised on any failure to fetch/parse an Archidekt deck."""


# ---- Deck fetch ------------------------------------------------------------


# Matches ``archidekt.com/decks/<id>`` with optional scheme, www, slug, trailing slash.
_DECK_URL_RE = re.compile(
    r"""(?:https?://)?               # optional scheme
        (?:www\.)?archidekt\.com     # host
        /decks/
        (?P<id>\d+)                  # the integer id
        (?:/[^?\s#]*)?               # optional /slug
        /?                           # optional trailing slash
        (?:[?#].*)?                  # optional query / hash
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_deck_id(deck_id_or_url: str | int) -> int:
    """Coerce a raw int, integer-string, or Archidekt URL to an integer id.

    Raises ``ArchidektError`` if no integer id can be found.
    """
    if isinstance(deck_id_or_url, int):
        if deck_id_or_url <= 0:
            raise ArchidektError(f"Deck id must be positive (got {deck_id_or_url})")
        return deck_id_or_url

    if not isinstance(deck_id_or_url, str):
        raise ArchidektError(f"Cannot parse deck id from {type(deck_id_or_url).__name__}")

    text = deck_id_or_url.strip()
    if not text:
        raise ArchidektError("Empty deck id or URL")

    # Plain integer string.
    if text.isdigit():
        return int(text)

    m = _DECK_URL_RE.search(text)
    if m:
        return int(m.group("id"))

    raise ArchidektError(
        "Could not find an Archidekt deck id in the input. "
        "Expected a number like 12345 or a URL like "
        "https://archidekt.com/decks/12345/my-deck."
    )


# Archidekt encodes colors as full-name strings ("White", "Blue"...). Normalize
# to the single-letter WUBRG used everywhere else in this codebase.
_COLOR_NAME_TO_LETTER = {
    "white": "W",
    "blue": "U",
    "black": "B",
    "red": "R",
    "green": "G",
    "colorless": "C",
    "w": "W",
    "u": "U",
    "b": "B",
    "r": "R",
    "g": "G",
}


def _normalize_colors(raw: list | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for c in raw:
        if not isinstance(c, str):
            continue
        letter = _COLOR_NAME_TO_LETTER.get(c.strip().lower())
        if letter and letter not in out:
            out.append(letter)
    return out


def _build_type_line(oracle_card) -> str | None:
    """Reconstruct a Scryfall-ish type line from the OracleCard fields."""
    if oracle_card is None:
        return None
    super_types = getattr(oracle_card, "super_types", None) or []
    types = getattr(oracle_card, "types", None) or []
    sub_types = getattr(oracle_card, "sub_types", None) or []
    # Both super_types/types and sub_types may be the unhelpful default `list`
    # (the class itself) if pyrchidekt sees missing fields. Guard for that.
    if not isinstance(super_types, list):
        super_types = []
    if not isinstance(types, list):
        types = []
    if not isinstance(sub_types, list):
        sub_types = []

    left = " ".join(str(t) for t in (list(super_types) + list(types)) if t)
    right = " ".join(str(t) for t in sub_types if t)
    if not left and not right:
        return None
    if right:
        return f"{left} — {right}".strip(" —")
    return left or None


def _pick_category(arch_card) -> str:
    """Choose a single category label for display."""
    cats = getattr(arch_card, "categories", None) or []
    if isinstance(cats, list) and cats:
        # Prefer "Commander" if present, else first listed.
        for c in cats:
            if isinstance(c, str) and c.lower() == "commander":
                return "Commander"
        for c in cats:
            if isinstance(c, str) and c:
                return c
    default = getattr(
        getattr(getattr(arch_card, "card", None), "oracle_card", None),
        "default_category",
        None,
    )
    if isinstance(default, str) and default:
        return default
    return "Mainboard"


def fetch_deck(deck_id_or_url: str | int) -> FetchedDeck:
    """Fetch a public Archidekt deck and normalize it to ``FetchedDeck``.

    Accepts a plain integer id (``12345``), an integer-string (``"12345"``),
    or any Archidekt deck URL. Raises ``ArchidektError`` on parse failure,
    network failure, or pyrchidekt-side issues.
    """
    deck_id = parse_deck_id(deck_id_or_url)

    # Import locally so the rest of the app boots cleanly even if pyrchidekt
    # is somehow unimportable (e.g. broken venv during a deploy).
    try:
        from pyrchidekt.api import getDeckById
    except ImportError as exc:  # pragma: no cover - defensive
        raise ArchidektError(f"pyrchidekt is not installed: {exc}") from exc

    try:
        raw = getDeckById(deck_id)
    except RuntimeError as exc:
        # pyrchidekt raises RuntimeError on 404/unknown failures. The default
        # 404 message names the id but reads awkwardly — surface a friendlier
        # version.
        msg = str(exc)
        if "not a valid deck" in msg or "private" in msg:
            raise ArchidektError(
                f"Deck {deck_id} could not be found. It may be private, "
                f"deleted, or not exist."
            ) from exc
        raise ArchidektError(msg) from exc
    except Exception as exc:
        # Wrap network errors (requests.ConnectionError, etc.) into our type.
        raise ArchidektError(
            f"Could not reach Archidekt to fetch deck {deck_id}. "
            f"Check your internet connection. ({exc.__class__.__name__})"
        ) from exc

    cards: list[FetchedDeckCard] = []
    for arch_card in getattr(raw, "cards", []) or []:
        inner = getattr(arch_card, "card", None)
        oracle = getattr(inner, "oracle_card", None) if inner is not None else None

        name = (getattr(oracle, "name", None) if oracle else None) or "Unknown card"
        scryfall_id = getattr(inner, "uid", None) if inner is not None else None
        if isinstance(scryfall_id, str) and not scryfall_id:
            scryfall_id = None

        cmc_raw = getattr(oracle, "cmc", None) if oracle else None
        # pyrchidekt uses -1 as the "missing" sentinel for cmc.
        if isinstance(cmc_raw, (int, float)) and cmc_raw >= 0:
            cmc_val: float | None = float(cmc_raw)
        else:
            cmc_val = None

        mana_cost_raw = getattr(oracle, "mana_cost", None) if oracle else None
        mana_cost = mana_cost_raw if isinstance(mana_cost_raw, str) and mana_cost_raw else None

        cards.append(
            FetchedDeckCard(
                name=name,
                oracle_id=None,  # filled in by services/archidekt.py from local cache
                scryfall_id=scryfall_id,
                quantity=int(getattr(arch_card, "quantity", 0) or 0),
                category=_pick_category(arch_card),
                cmc=cmc_val,
                mana_cost=mana_cost,
                type_line=_build_type_line(oracle),
                colors=_normalize_colors(getattr(oracle, "colors", None) if oracle else None),
            )
        )

    owner = getattr(raw, "owner", None)
    owner_name = getattr(owner, "username", None) if owner is not None else None

    deck_name = getattr(raw, "name", None) or f"Archidekt deck #{deck_id}"
    return FetchedDeck(
        id=int(getattr(raw, "id", deck_id) or deck_id),
        name=deck_name,
        owner=owner_name if isinstance(owner_name, str) and owner_name else None,
        url=f"https://archidekt.com/decks/{deck_id}",
        cards=cards,
    )
