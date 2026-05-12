"""Collection browse, card detail, and edit/bulk-edit routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from card_catalog.config import settings as app_settings
from card_catalog.db.models import CollectionEntry
from card_catalog.db.session import get_db
from card_catalog.services import collection as svc
from card_catalog.services import settings as settings_svc
from card_catalog.domain.enums import CONDITION_LABELS, Condition, Finish

router = APIRouter(tags=["collection"])
templates = Jinja2Templates(directory=str(app_settings.templates_dir))


# ---- Helpers ----------------------------------------------------------------


def _parse_card_faces(card_faces_json: str | None) -> list[dict[str, Any]]:
    if not card_faces_json:
        return []
    try:
        data = json.loads(card_faces_json)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _parse_color_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    if isinstance(data, list):
        return [str(c) for c in data]
    return []


def _parse_legalities(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _toast_header(title: str, body: str = "", kind: str = "success") -> str:
    return json.dumps({"title": title, "body": body, "kind": kind})


def _filter_from_source(qp: Any, db: Session) -> svc.FilterSpec:
    """Build FilterSpec from a MultiDict-like object (query_params or form data)."""
    default_page_size = int(settings_svc.get(db, "page_size") or 60)

    def multi(name: str) -> list[str]:
        # Both Starlette QueryParams and FormData support getlist().
        getter = getattr(qp, "getlist", None)
        if getter is None:
            return []
        return list(getter(name))

    raw: dict[str, Any] = {
        "q": qp.get("q") or None,
        "set_code": multi("set_code"),
        "colors": multi("colors"),
        "colors_mode": qp.get("colors_mode") or "includes",
        "rarity": multi("rarity"),
        "finish": multi("finish"),
        "condition": multi("condition"),
        "language": multi("language"),
        "tags": multi("tags"),
        "cmc_min": qp.get("cmc_min") or None,
        "cmc_max": qp.get("cmc_max") or None,
        "price_min": qp.get("price_min") or None,
        "price_max": qp.get("price_max") or None,
        "qty_min": qp.get("qty_min") or None,
        "qty_max": qp.get("qty_max") or None,
        "for_trade": qp.get("for_trade"),
        "sort": qp.get("sort") or "name",
        "page": qp.get("page") or 1,
        "page_size": qp.get("page_size") or default_page_size,
    }
    return svc.FilterSpec(**raw)


def _filter_from_request(request: Request, db: Session) -> svc.FilterSpec:
    return _filter_from_source(request.query_params, db)


def _editor_context(db: Session) -> dict[str, Any]:
    return {
        "conditions": [(c.value, CONDITION_LABELS[c]) for c in Condition],
        "finishes": [(f.value, f.value.capitalize()) for f in Finish],
        "tags": svc.list_tags(db),
    }


# ---- Browse page ------------------------------------------------------------


@router.get("/collection", response_class=HTMLResponse)
async def collection_page(request: Request, db: Session = Depends(get_db)):
    filt = _filter_from_request(request, db)
    total_in_db = svc.total_collection_size(db)

    result = svc.search(db, filt)
    view = request.query_params.get("view") or settings_svc.get(db, "default_view") or "grid"
    owner_name = settings_svc.get(db, "collection_owner_name") or "Collector"
    currency = settings_svc.get(db, "display_currency") or "USD"

    ctx = {
        "active_nav": "collection",
        "owner_name": owner_name,
        "currency": currency,
        "view": view if view in ("grid", "list") else "grid",
        "filt": filt,
        "result": result,
        "sets": svc.distinct_sets(db, limit=80),
        "languages": svc.distinct_languages(db),
        "tags_all": svc.list_tags(db),
        "total_in_db": total_in_db,
        "editor": _editor_context(db),
    }
    return templates.TemplateResponse(request, "collection.html", ctx)


@router.get("/collection/table", response_class=HTMLResponse)
async def collection_table(request: Request, db: Session = Depends(get_db)):
    """HTMX fragment endpoint — swaps the grid/list region. Pushes URL for back-button."""
    filt = _filter_from_request(request, db)
    result = svc.search(db, filt)
    view = request.query_params.get("view") or settings_svc.get(db, "default_view") or "grid"
    view = view if view in ("grid", "list") else "grid"
    currency = settings_svc.get(db, "display_currency") or "USD"

    ctx = {
        "filt": filt,
        "result": result,
        "view": view,
        "currency": currency,
    }
    template = "partials/card_grid.html" if view == "grid" else "partials/card_table.html"
    response = templates.TemplateResponse(request, template, ctx)
    # Push the canonical querystring (adds &view= so refresh respects current view).
    qs = filt.querystring()
    push = f"/collection?view={view}" + (f"&{qs}" if qs else "")
    response.headers["HX-Push-Url"] = push
    return response


# ---- Card detail ------------------------------------------------------------


def _detail_context(db: Session, card, scryfall_id: str) -> dict[str, Any]:
    entries = svc.get_entries_for_card(db, scryfall_id)
    primary_finish = entries[0].finish if entries else "nonfoil"
    latest = svc.latest_price_for_card(db, card, primary_finish)
    points = svc.price_history_points(db, card, primary_finish, days=30)
    currency = settings_svc.get(db, "display_currency") or "USD"
    return {
        "card": card,
        "entries": entries,
        "latest": latest,
        "points": points,
        "currency": currency,
        "colors": _parse_color_list(card.colors),
        "color_identity": _parse_color_list(card.color_identity),
        "faces": _parse_card_faces(card.card_faces_json),
        "legalities": _parse_legalities(card.legalities_json),
        "editor": _editor_context(db),
    }


@router.get("/cards/{scryfall_id}", response_class=HTMLResponse)
async def card_detail(
    request: Request,
    scryfall_id: str,
    db: Session = Depends(get_db),
    slideover: int | None = None,
):
    card = svc.get_card(db, scryfall_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    ctx = _detail_context(db, card, scryfall_id)
    if slideover:
        return templates.TemplateResponse(request, "partials/card_slideover.html", ctx)
    ctx["active_nav"] = "collection"
    ctx["owner_name"] = settings_svc.get(db, "collection_owner_name") or "Collector"
    return templates.TemplateResponse(request, "card_detail.html", ctx)


# ---- Entry edit -------------------------------------------------------------


@router.get("/collection/entries/{entry_id}/edit", response_class=HTMLResponse)
async def entry_edit_form(request: Request, entry_id: int, db: Session = Depends(get_db)):
    entry = db.get(CollectionEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/card_edit_form.html",
        {"entry": entry, "editor": _editor_context(db)},
    )


@router.patch("/collection/entries/{entry_id}", response_class=HTMLResponse)
async def entry_patch(request: Request, entry_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    fields = {k: v for k, v in form.items() if k != "_method"}
    try:
        entry = svc.update_entry(db, entry_id, fields)
    except svc.EditError as e:
        return Response(
            content=str(e),
            status_code=400,
            headers={"HX-Toast": _toast_header("Update failed", str(e), kind="error")},
        )
    except KeyError:
        raise HTTPException(status_code=404)

    if entry is None:
        # quantity went to 0 → row should disappear
        return Response(
            "",
            status_code=200,
            headers={
                "HX-Toast": _toast_header(
                    "Entry deleted", "Quantity hit zero.", kind="success"
                )
            },
        )

    # Inline edits originate inside the slide-over's "Your copies" section, so we
    # return the corresponding copy-row partial (a div, not a <tr>).
    currency = settings_svc.get(db, "display_currency") or "USD"
    response = templates.TemplateResponse(
        request,
        "partials/copy_row.html",
        {"entry": entry, "currency": currency},
    )
    response.headers["HX-Toast"] = _toast_header("Saved", f"{entry.card.name} updated.")
    return response


# POST alias for environments where PATCH isn't easily sent — HTMX supports
# hx-patch natively so this is rarely needed but cheap insurance.
@router.post("/collection/entries/{entry_id}", response_class=HTMLResponse)
async def entry_post(request: Request, entry_id: int, db: Session = Depends(get_db)):
    return await entry_patch(request, entry_id, db)


@router.delete("/collection/entries/{entry_id}", response_class=HTMLResponse)
async def entry_delete(request: Request, entry_id: int, db: Session = Depends(get_db)):
    try:
        svc.delete_entry(db, entry_id)
    except KeyError:
        raise HTTPException(status_code=404)
    return Response(
        "",
        status_code=200,
        headers={"HX-Toast": _toast_header("Deleted", "Entry removed from collection.")},
    )


# ---- Bulk edit --------------------------------------------------------------


@router.post("/collection/bulk-edit", response_class=HTMLResponse)
async def bulk_edit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids_raw = str(form.get("entry_ids") or "")
    entry_ids = [s for s in ids_raw.split(",") if s.strip()]
    action = str(form.get("action") or "update").lower()

    if action == "delete":
        n = svc.bulk_delete(db, entry_ids)
        msg = f"Deleted {n} entr{'y' if n == 1 else 'ies'}."
    elif action == "tag":
        tag_name = str(form.get("tag_name") or "").strip()
        if not tag_name:
            return Response(
                "Tag name required.",
                status_code=400,
                headers={"HX-Toast": _toast_header("Tag failed", "Provide a tag name.", "error")},
            )
        n = svc.bulk_add_tag(db, entry_ids, tag_name)
        msg = f"Tagged {n} entr{'y' if n == 1 else 'ies'} with “{tag_name}”."
    else:
        patch: dict[str, Any] = {}
        for k in ("condition", "finish", "language", "for_trade", "altered", "misprint", "notes"):
            v = form.get(k)
            if v is not None and str(v).strip() != "":
                patch[k] = v
        try:
            n = svc.bulk_update(db, entry_ids, patch)
        except svc.EditError as e:
            return Response(
                str(e),
                status_code=400,
                headers={"HX-Toast": _toast_header("Bulk update failed", str(e), "error")},
            )
        msg = f"Updated {n} entr{'y' if n == 1 else 'ies'}."

    # After a bulk op, refresh the table fragment. Filter values are in the form
    # (HTMX hx-include) since this is a POST.
    filt = _filter_from_source(form, db)
    result = svc.search(db, filt)
    view = (
        form.get("view")
        or request.query_params.get("view")
        or settings_svc.get(db, "default_view")
        or "grid"
    )
    view = view if view in ("grid", "list") else "grid"
    currency = settings_svc.get(db, "display_currency") or "USD"
    template = "partials/card_grid.html" if view == "grid" else "partials/card_table.html"
    response = templates.TemplateResponse(
        request,
        template,
        {"filt": filt, "result": result, "view": view, "currency": currency},
    )
    response.headers["HX-Toast"] = _toast_header("Bulk action complete", msg, "success")
    return response
