"""Header-tolerant Manabox CSV parser.

Reads a Manabox-style export (or any close cousin), normalizes headers, and
emits a stream of `ImportRow` dataclass records.

Confirmed canonical spellings (per `sboulema/ManaBoxImporter` CsvHelper attrs):
    Set code, Set name, Collector number, Scryfall ID, Name, Quantity

Likely-but-unconfirmed spellings tolerated case-insensitively, with common
aliases:
    Foil, Condition, Language, Purchase price, Purchase currency,
    Misprint, Altered

Policy: unknown columns produce a warning, never an error. Rows with no
Scryfall ID *and* no (set_code + collector_number) pair are dropped with a
warning so the preview can still proceed.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import IO, Any

from card_catalog.domain.enums import Condition, Finish, normalize_language


# Canonical key -> tuple of accepted header spellings (all compared case-insensitively
# after lowercasing + collapsing whitespace).
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "card name", "card"),
    "set_code": ("set code", "setcode", "set", "edition code", "edition"),
    "set_name": ("set name", "setname", "edition name"),
    "collector_number": (
        "collector number",
        "collectornumber",
        "collector #",
        "collector no",
        "collector",
        "card number",
        "number",
        "cn",
    ),
    "scryfall_id": (
        "scryfall id",
        "scryfallid",
        "scryfall_id",
        "scryfall uuid",
        "scryfall guid",
    ),
    "quantity": ("quantity", "qty", "count", "amount"),
    "foil": ("foil", "finish", "printing", "foiling"),
    "condition": ("condition", "cond", "grade"),
    "language": ("language", "lang"),
    "purchase_price": (
        "purchase price",
        "price",
        "paid",
        "cost",
        "acquired price",
    ),
    "purchase_currency": ("purchase currency", "currency", "price currency"),
    "misprint": ("misprint", "is misprint"),
    "altered": ("altered", "is altered"),
}


@dataclass
class ImportRow:
    name: str
    set_code: str
    collector_number: str
    scryfall_id: str | None
    quantity: int
    finish: str  # 'nonfoil' | 'foil' | 'etched'
    condition: str  # NM/LP/MP/HP/DMG
    language: str  # ISO code
    purchase_price: float | None = None
    purchase_currency: str | None = None
    altered: bool = False
    misprint: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def lookup_key(self) -> tuple[str, str]:
        return (self.set_code, self.collector_number)


def _normalize_header(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _build_header_map(fieldnames: list[str]) -> tuple[dict[str, str], list[str]]:
    """Return (canonical_key -> original_header) and a list of warnings."""
    warnings: list[str] = []
    # Build reverse map: normalized -> canonical
    rev: dict[str, str] = {}
    for canonical, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            rev[_normalize_header(alias)] = canonical

    mapping: dict[str, str] = {}
    for original in fieldnames:
        n = _normalize_header(original)
        if not n:
            continue
        canonical = rev.get(n)
        if canonical is None:
            warnings.append(f"Unknown column “{original}” — ignored.")
            continue
        if canonical in mapping:
            warnings.append(
                f"Duplicate column for {canonical}: “{mapping[canonical]}” and “{original}”;"
                " keeping the first."
            )
            continue
        mapping[canonical] = original
    return mapping, warnings


def _coerce_int(value: Any, default: int = 1) -> int:
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Manabox uses InvariantCulture (`.` decimal) but tolerate stray symbols.
    for ch in ("$", "€", "£", "¥"):
        s = s.replace(ch, "")
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _coerce_bool(value: Any) -> bool:
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in {"1", "true", "yes", "y", "t"}


def _normalize_finish(value: Any, default: str = Finish.NONFOIL.value) -> str:
    s = (str(value or "").strip().lower())
    if not s:
        return default
    if s in {"etched", "etched foil", "foil etched"}:
        return Finish.ETCHED.value
    if s in {"foil"}:
        return Finish.FOIL.value
    if s in {"normal", "nonfoil", "non-foil", "non foil", ""}:
        return Finish.NONFOIL.value
    # Unrecognized: fall back to default rather than fail the row.
    return default


def parse(
    file_obj: IO[Any] | bytes | str,
    *,
    default_condition: str = "NM",
    default_finish: str = "nonfoil",
    default_language: str = "en",
) -> tuple[list[ImportRow], list[str]]:
    """Parse a Manabox CSV. Returns (rows, warnings).

    `file_obj` may be a text file, a binary file (decoded as utf-8-sig), bytes,
    or a string. Rows missing both Scryfall ID and (set + collector number)
    are dropped with a warning.
    """
    text = _coerce_text(file_obj)
    if not text.strip():
        return [], ["The uploaded file appears to be empty."]

    # csv.DictReader handles the header row; we keep our own normalization.
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [fn for fn in (reader.fieldnames or []) if fn is not None]
    if not fieldnames:
        return [], ["No header row found — is this a CSV?"]

    header_map, warnings = _build_header_map(fieldnames)

    # Fail fast on the must-haves: we need a way to *identify* the card.
    if "scryfall_id" not in header_map and not (
        "set_code" in header_map and "collector_number" in header_map
    ):
        warnings.append(
            "CSV missing identifier columns. Need either “Scryfall ID” or both"
            " “Set code” and “Collector number”."
        )
        return [], warnings
    if "quantity" not in header_map:
        warnings.append("CSV missing “Quantity” — defaulting every row to 1.")

    rows: list[ImportRow] = []
    dropped = 0

    for idx, raw in enumerate(reader, start=2):  # start=2 (header is row 1)
        # Extract using header_map; missing canonical keys → None.
        def col(name: str) -> Any:
            original = header_map.get(name)
            if original is None:
                return None
            return raw.get(original)

        sid_raw = (col("scryfall_id") or "").strip()
        set_code = (col("set_code") or "").strip().lower()
        collector_number = (col("collector_number") or "").strip()
        name = (col("name") or "").strip()

        # Reject if there's no way to identify this card.
        if not sid_raw and not (set_code and collector_number):
            dropped += 1
            warnings.append(
                f"Row {idx} (“{name or 'unnamed'}”): no Scryfall ID and no set+number — skipped."
            )
            continue

        quantity = max(1, _coerce_int(col("quantity"), default=1))

        finish = _normalize_finish(col("foil"), default=default_finish)

        cond_raw = col("condition")
        try:
            condition = Condition.from_manabox(cond_raw).value if cond_raw else default_condition
        except Exception:  # noqa: BLE001
            condition = default_condition

        lang_raw = col("language")
        language = normalize_language(lang_raw) if lang_raw else default_language

        purchase_price = _coerce_float(col("purchase_price"))
        purchase_currency_raw = col("purchase_currency")
        purchase_currency = (
            str(purchase_currency_raw).strip().upper() if purchase_currency_raw else None
        ) or None

        altered = _coerce_bool(col("altered"))
        misprint = _coerce_bool(col("misprint"))

        rows.append(
            ImportRow(
                name=name,
                set_code=set_code,
                collector_number=collector_number,
                scryfall_id=sid_raw or None,
                quantity=quantity,
                finish=finish,
                condition=condition,
                language=language,
                purchase_price=purchase_price,
                purchase_currency=purchase_currency,
                altered=altered,
                misprint=misprint,
                raw=dict(raw),
            )
        )

    if dropped:
        warnings.insert(0, f"Skipped {dropped} row(s) missing identifiers.")
    if not rows and not any(w.startswith("CSV missing") for w in warnings):
        warnings.append("No importable rows were found in this CSV.")

    return rows, warnings


def _coerce_text(file_obj: IO[Any] | bytes | str) -> str:
    """Accept many shapes; return decoded text. Strips BOM."""
    if isinstance(file_obj, bytes):
        data = file_obj
    elif isinstance(file_obj, str):
        return file_obj.lstrip("﻿")
    else:
        chunk = file_obj.read()
        if isinstance(chunk, bytes):
            data = chunk
        else:
            return chunk.lstrip("﻿")
    # Tolerate BOM.
    return data.decode("utf-8-sig", errors="replace")


__all__ = ["ImportRow", "parse"]
