from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from card_catalog.config import settings as app_settings
from card_catalog.db.models import (
    CollectionEntry,
    ImportRun,
    PriceHistory,
    ScryfallCard,
)
from card_catalog.db.session import get_db
from card_catalog.services import settings as settings_svc

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(app_settings.templates_dir))


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    total_qty = db.scalar(select(func.coalesce(func.sum(CollectionEntry.quantity), 0))) or 0
    unique_printings = db.scalar(select(func.count(CollectionEntry.id))) or 0
    unique_oracles = (
        db.scalar(
            select(func.count(func.distinct(ScryfallCard.oracle_id)))
            .select_from(CollectionEntry)
            .join(ScryfallCard, ScryfallCard.scryfall_id == CollectionEntry.scryfall_id)
        )
        or 0
    )

    # Estimated collection value via latest market price per (tcgplayer_id, sub_type).
    # Computed in SQL to keep the dashboard snappy.
    latest_prices = (
        select(
            PriceHistory.tcgplayer_id,
            PriceHistory.sub_type,
            func.max(PriceHistory.as_of).label("as_of"),
        )
        .group_by(PriceHistory.tcgplayer_id, PriceHistory.sub_type)
        .subquery()
    )
    value_query = (
        select(
            func.coalesce(
                func.sum(
                    CollectionEntry.quantity
                    * func.coalesce(PriceHistory.market_price, PriceHistory.mid_price, 0.0)
                ),
                0.0,
            )
        )
        .select_from(CollectionEntry)
        .join(ScryfallCard, ScryfallCard.scryfall_id == CollectionEntry.scryfall_id)
        .join(
            PriceHistory,
            (
                PriceHistory.tcgplayer_id
                == func.coalesce(ScryfallCard.tcgplayer_etched_id, ScryfallCard.tcgplayer_id)
            )
            & (
                PriceHistory.sub_type
                == func.iif(CollectionEntry.finish == "foil", "Foil", "Normal")
            ),
            isouter=True,
        )
        .join(
            latest_prices,
            (latest_prices.c.tcgplayer_id == PriceHistory.tcgplayer_id)
            & (latest_prices.c.sub_type == PriceHistory.sub_type)
            & (latest_prices.c.as_of == PriceHistory.as_of),
            isouter=True,
        )
    )
    total_value = db.scalar(value_query) or 0.0

    by_rarity = (
        db.execute(
            select(ScryfallCard.rarity, func.sum(CollectionEntry.quantity))
            .join(ScryfallCard, ScryfallCard.scryfall_id == CollectionEntry.scryfall_id)
            .group_by(ScryfallCard.rarity)
        )
        .all()
    )
    rarity_counts = {r: int(q or 0) for r, q in by_rarity}

    by_color = (
        db.execute(
            select(ScryfallCard.colors, func.sum(CollectionEntry.quantity))
            .join(ScryfallCard, ScryfallCard.scryfall_id == CollectionEntry.scryfall_id)
            .group_by(ScryfallCard.colors)
        )
        .all()
    )
    color_counts: dict[str, int] = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0, "M": 0}
    for colors_json, qty in by_color:
        q = int(qty or 0)
        if not colors_json or colors_json in ("[]", "null"):
            color_counts["C"] += q
            continue
        # Cheap parse — these are short JSON strings like ["U","R"]
        letters = [c for c in colors_json if c in "WUBRG"]
        if len(letters) >= 2:
            color_counts["M"] += q
        elif len(letters) == 1:
            color_counts[letters[0]] += q

    recent_imports = (
        db.execute(
            select(ImportRun).order_by(ImportRun.started_at.desc()).limit(5)
        )
        .scalars()
        .all()
    )

    recently_added = (
        db.execute(
            select(CollectionEntry).order_by(CollectionEntry.created_at.desc()).limit(8)
        )
        .scalars()
        .all()
    )

    owner_name = settings_svc.get(db, "collection_owner_name") or "Collector"

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_nav": "dashboard",
            "owner_name": owner_name,
            "total_qty": int(total_qty),
            "unique_printings": int(unique_printings),
            "unique_oracles": int(unique_oracles),
            "total_value": float(total_value),
            "rarity_counts": rarity_counts,
            "color_counts": color_counts,
            "recent_imports": recent_imports,
            "recently_added": recently_added,
        },
    )
