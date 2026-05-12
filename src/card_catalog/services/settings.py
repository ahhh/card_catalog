"""Settings k/v service. Keys live in the DB; defaults are seeded on first use."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from card_catalog.db.models import Setting


SETTING_DEFAULTS: dict[str, str] = {
    "display_currency": "USD",
    "default_condition": "NM",
    "default_finish": "nonfoil",
    "default_language": "en",
    "scryfall_user_agent": "card_catalog/0.1 (personal collection tracker)",
    "scryfall_delay_ms": "100",
    "tcgplayer_public_key": "",
    "tcgplayer_private_key": "",
    "collection_owner_name": "Collector",
    "page_size": "60",
    "default_view": "grid",
}


@dataclass
class SettingSpec:
    key: str
    label: str
    description: str
    kind: str  # "text" | "select" | "secret" | "number"
    options: list[tuple[str, str]] | None = None
    placeholder: str = ""
    group: str = "general"


SETTING_SPECS: list[SettingSpec] = [
    SettingSpec(
        key="collection_owner_name",
        label="Collection name",
        description="Shown in the header. Make it yours.",
        kind="text",
        placeholder="e.g. Dan's Vault",
        group="profile",
    ),
    SettingSpec(
        key="display_currency",
        label="Display currency",
        description="Prices from Scryfall and TCGCSV are always USD. This controls the symbol.",
        kind="select",
        options=[("USD", "USD ($)"), ("EUR", "EUR (€)"), ("GBP", "GBP (£)")],
        group="profile",
    ),
    SettingSpec(
        key="default_view",
        label="Default collection view",
        description="Grid feels like a binder. List packs more rows on screen.",
        kind="select",
        options=[("grid", "Grid"), ("list", "List")],
        group="profile",
    ),
    SettingSpec(
        key="page_size",
        label="Rows per page",
        description="Higher values mean fewer page turns but slower renders on big collections.",
        kind="number",
        group="profile",
    ),
    SettingSpec(
        key="default_condition",
        label="Default condition",
        description="Applied to imported rows when the CSV doesn't say.",
        kind="select",
        options=[
            ("NM", "Near Mint"),
            ("LP", "Lightly Played"),
            ("MP", "Moderately Played"),
            ("HP", "Heavily Played"),
            ("DMG", "Damaged"),
        ],
        group="imports",
    ),
    SettingSpec(
        key="default_finish",
        label="Default finish",
        description="Used when the CSV omits the foil column.",
        kind="select",
        options=[("nonfoil", "Non-foil"), ("foil", "Foil"), ("etched", "Etched")],
        group="imports",
    ),
    SettingSpec(
        key="default_language",
        label="Default language",
        description="ISO code. Used when the import row has no language cell.",
        kind="text",
        placeholder="en",
        group="imports",
    ),
    SettingSpec(
        key="scryfall_user_agent",
        label="Scryfall User-Agent",
        description=(
            "Scryfall requires this header. Include a name or contact — see "
            "scryfall.com/docs/api."
        ),
        kind="text",
        placeholder="card_catalog/0.1 (you@example.com)",
        group="apis",
    ),
    SettingSpec(
        key="scryfall_delay_ms",
        label="Scryfall request delay (ms)",
        description="Floor for the per-endpoint rate limiter. Don't go below 100.",
        kind="number",
        group="apis",
    ),
    SettingSpec(
        key="tcgplayer_public_key",
        label="TCGplayer public key",
        description=(
            "Optional. TCGCSV provides daily prices without this. New TCGplayer "
            "API access is no longer being granted, but the field is here in case "
            "you have an existing key."
        ),
        kind="text",
        placeholder="",
        group="apis",
    ),
    SettingSpec(
        key="tcgplayer_private_key",
        label="TCGplayer private key",
        description="Stored in plaintext. This app is local-only by design.",
        kind="secret",
        placeholder="",
        group="apis",
    ),
]


SETTING_GROUPS = [
    ("profile", "Your collection", "Identity and how the catalog presents itself."),
    ("imports", "Imports & defaults", "Fallback values when CSV rows are sparse."),
    ("apis", "External APIs", "Polite delays and optional keys for outside services."),
]


def ensure_defaults(db: Session) -> None:
    existing = {s.key for s in db.scalars(select(Setting)).all()}
    for key, default in SETTING_DEFAULTS.items():
        if key not in existing:
            db.add(Setting(key=key, value=default))
    db.commit()


def get_all(db: Session) -> dict[str, str]:
    rows = db.scalars(select(Setting)).all()
    out = {row.key: (row.value or "") for row in rows}
    for k, v in SETTING_DEFAULTS.items():
        out.setdefault(k, v)
    return out


def get(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    if default is not None:
        return default
    return SETTING_DEFAULTS.get(key)


def set_many(db: Session, updates: dict[str, str]) -> None:
    for key, value in updates.items():
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value
    db.commit()
