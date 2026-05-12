"""Archidekt HTTP layer.

Three routes:

- ``GET /archidekt`` renders the dual-flow page (export + reconcile).
- ``GET /archidekt/export.csv`` streams the collection in the format
  Archidekt's web importer accepts.
- ``POST /archidekt/reconcile`` fetches a public deck via pyrchidekt and
  returns an HTML fragment with the owned/missing breakdown.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from card_catalog.clients.archidekt import ArchidektError, fetch_deck
from card_catalog.config import settings as app_settings
from card_catalog.db.models import CollectionEntry
from card_catalog.db.session import get_db
from card_catalog.services import archidekt as svc
from card_catalog.services import settings as settings_svc

router = APIRouter(prefix="/archidekt", tags=["archidekt"])
templates = Jinja2Templates(directory=str(app_settings.templates_dir))


@router.get("", response_class=HTMLResponse)
async def archidekt_page(request: Request, db: Session = Depends(get_db)):
    """The Archidekt landing page: Export + Reconcile, side by side."""
    distinct_printings = (
        db.scalar(select(func.count(func.distinct(CollectionEntry.scryfall_id)))) or 0
    )
    total_qty = db.scalar(select(func.coalesce(func.sum(CollectionEntry.quantity), 0))) or 0
    owner_name = settings_svc.get(db, "collection_owner_name") or None

    return templates.TemplateResponse(
        request,
        "archidekt.html",
        {
            "active_nav": "archidekt",
            "owner_name": owner_name,
            "distinct_printings": int(distinct_printings),
            "total_qty": int(total_qty),
        },
    )


@router.get("/export.csv")
async def archidekt_export_csv(db: Session = Depends(get_db)):
    """Generate the Quantity,Scryfall ID CSV.

    Filter integration (M8 polish item) is deferred — for now we export the
    whole collection. We pass an empty dict to the service so its signature
    is stable for future wiring.
    """
    body, filename = svc.export_filtered_collection(db, {})
    # Count non-header rows that hit Archidekt.
    written = max(body.count("\n") - 1, 0)

    # Sanity guard: skipped rows would only happen if a CollectionEntry had a
    # null scryfall_id, which our schema disallows — but we surface it via a
    # response header anyway so it's observable in DevTools.
    total_groups = (
        db.scalar(select(func.count(func.distinct(CollectionEntry.scryfall_id)))) or 0
    )
    skipped = max(int(total_groups) - written, 0)

    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Skipped-Rows": str(skipped),
            "Cache-Control": "no-store",
        },
    )


@router.post("/reconcile", response_class=HTMLResponse)
async def archidekt_reconcile(
    request: Request,
    db: Session = Depends(get_db),
    deck_input: str = Form(""),
):
    """Fetch a public deck and render the reconcile fragment.

    Synchronous network fetch — Archidekt typically responds in 1-2 seconds
    and an HTMX spinner covers the wait.
    """
    raw = (deck_input or "").strip()
    if not raw:
        return templates.TemplateResponse(
            request,
            "partials/archidekt_reconcile.html",
            {
                "error": "Paste an Archidekt deck URL or numeric id to begin.",
            },
            status_code=400,
        )

    try:
        deck = fetch_deck(raw)
    except ArchidektError as exc:
        return templates.TemplateResponse(
            request,
            "partials/archidekt_reconcile.html",
            {"error": str(exc)},
            status_code=400,
        )

    report = svc.reconcile_with_deck(db, deck)

    completion_pct = 0.0
    if report.total_needed > 0:
        completion_pct = round(
            (report.total_owned / report.total_needed) * 100.0, 1
        )

    return templates.TemplateResponse(
        request,
        "partials/archidekt_reconcile.html",
        {
            "report": report,
            "completion_pct": completion_pct,
            "error": None,
        },
    )
