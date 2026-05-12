"""TCGCSV daily prices: refresh trigger, job polling, history."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from card_catalog.clients.tcgcsv import TCGCSV, TCGCSVError
from card_catalog.utils import utc_now
from card_catalog.config import settings as app_settings
from card_catalog.db.models import CollectionEntry, ScryfallCard
from card_catalog.db.session import SessionLocal, get_db
from card_catalog.jobs.registry import JobStatus, registry
from card_catalog.jobs.runner import run_in_thread
from card_catalog.services import prices as prices_svc
from card_catalog.services import settings as settings_svc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/prices", tags=["prices"])
templates = Jinja2Templates(directory=str(app_settings.templates_dir))


# ---------- helpers ---------------------------------------------------------


def _format_utc(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%b %d, %H:%M UTC")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # TCGCSV emits "2026-05-12T20:01:23Z"; tolerate trailing Z.
        cleaned = value.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).replace(tzinfo=None)
    except ValueError:
        return None


def _job_duration_s(job) -> float | None:
    if not job.finished_at:
        if job.status == JobStatus.RUNNING:
            return (utc_now() - job.started_at).total_seconds()
        return None
    return (job.finished_at - job.started_at).total_seconds()


def _job_view(job) -> dict:
    """Flatten a JobState into the shape templates want."""
    return {
        "id": job.id,
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
        "label": job.label,
        "total": job.total,
        "done": job.done,
        "progress_pct": job.progress_pct,
        "detail": job.detail,
        "error": job.error,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "duration_s": _job_duration_s(job),
        "updates": int(job.extras.get("updates", 0)) if job.extras else 0,
        "mapped_sets": int(job.extras.get("mapped_sets", 0)) if job.extras else 0,
        "unmapped_sets": list(job.extras.get("unmapped_sets", []) or []) if job.extras else [],
        "is_terminal": job.is_terminal,
    }


# ---------- page ------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def prices_page(request: Request, db: Session = Depends(get_db)):
    # Counts — never hit the network here.
    tracked = prices_svc.tracked_tcgplayer_count(db)
    total_entries = (
        db.scalar(select(func.coalesce(func.sum(CollectionEntry.quantity), 0))) or 0
    )
    has_any_collection = (db.scalar(select(func.count(CollectionEntry.id))) or 0) > 0
    has_any_cards = (db.scalar(select(func.count(ScryfallCard.scryfall_id))) or 0) > 0

    try:
        collection_value = prices_svc.collection_value_estimate(db)
    except Exception:  # noqa: BLE001
        log.exception("collection_value_estimate failed")
        collection_value = 0.0

    last_refresh = prices_svc.latest_refresh_at(db)

    # TCGCSV's last-updated.txt is fetched lazily on POST refresh; we surface
    # the value we cached on the most recent terminal refresh job, if any.
    tcgcsv_last_updated: datetime | None = None
    tcgcsv_last_updated_raw = ""
    for j in registry.list(kind="prices_refresh", limit=20):
        raw = (j.extras or {}).get("tcgcsv_last_updated")
        if raw:
            tcgcsv_last_updated = _parse_iso(raw)
            tcgcsv_last_updated_raw = raw
            break

    # Recent jobs (most recent first).
    recent_jobs = [_job_view(j) for j in registry.list(kind="prices_refresh", limit=8)]
    latest_terminal = next((j for j in recent_jobs if j["is_terminal"]), None)
    unmapped_sets: list[str] = latest_terminal["unmapped_sets"] if latest_terminal else []

    # An active job, if any — used to skip rendering the idle button.
    active = registry.find_active("prices_refresh")
    active_view = _job_view(active) if active else None

    data_available = bool(
        tcgcsv_last_updated and last_refresh and tcgcsv_last_updated > last_refresh
    )

    owner_name = settings_svc.get(db, "collection_owner_name") or "Collector"

    return templates.TemplateResponse(
        request,
        "prices.html",
        {
            "active_nav": "prices",
            "owner_name": owner_name,
            "tracked_cards": tracked,
            "total_entries": int(total_entries),
            "has_any_collection": has_any_collection,
            "has_any_cards": has_any_cards,
            "collection_value": float(collection_value),
            "last_refresh": last_refresh,
            "last_refresh_str": _format_utc(last_refresh),
            "tcgcsv_last_updated": tcgcsv_last_updated,
            "tcgcsv_last_updated_str": _format_utc(tcgcsv_last_updated),
            "tcgcsv_last_updated_raw": tcgcsv_last_updated_raw,
            "data_available": data_available,
            "recent_jobs": recent_jobs,
            "unmapped_sets": unmapped_sets,
            "active_job": active_view,
            "overrides_path": "src/card_catalog/data/tcgcsv_group_overrides.json",
        },
    )


# ---------- refresh trigger ------------------------------------------------


def _run_refresh(job) -> None:
    """Thread target. Opens its own session — the request's `db` is gone by now."""
    db = SessionLocal()
    client = TCGCSV()
    try:
        # Stash TCGCSV's last-updated marker before we start so the UI can show
        # data-availability staleness.
        try:
            job.extras["tcgcsv_last_updated"] = client.last_updated()
        except TCGCSVError as exc:
            log.warning("could not fetch last-updated.txt: %s", exc)
        prices_svc.refresh_collection_prices(db, job, client)
    finally:
        client.close()
        db.close()


@router.post("/refresh", response_class=HTMLResponse)
async def refresh_trigger(request: Request, db: Session = Depends(get_db)):
    # Guard: nothing to price.
    has_any = (db.scalar(select(func.count(CollectionEntry.id))) or 0) > 0
    if not has_any:
        raise HTTPException(status_code=400, detail="Add some cards to your collection first.")

    job = run_in_thread(
        kind="prices_refresh",
        label="Refreshing TCGCSV prices",
        target=_run_refresh,
        total=0,  # services/prices fills this in after fetching the groups index
        singleton=True,
    )

    return templates.TemplateResponse(
        request,
        "partials/prices_jobcard.html",
        {
            "job": _job_view(job),
            "poll_url": f"/prices/jobs/{job.id}",
        },
    )


# ---------- polled job status ----------------------------------------------


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_status(request: Request, job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    view = _job_view(job)
    response = templates.TemplateResponse(
        request,
        "partials/prices_jobcard.html",
        {
            "job": view,
            "poll_url": f"/prices/jobs/{job.id}" if not job.is_terminal else None,
            "show_reload": job.is_terminal,
        },
    )
    # Fire a one-shot success toast as soon as we observe completion.
    if job.is_terminal:
        kind = "success" if job.status == JobStatus.COMPLETED else "error"
        title = "Prices refreshed" if kind == "success" else "Refresh failed"
        body = (
            f"Updated {view['updates']:,} rows across {view['mapped_sets']} sets"
            if kind == "success"
            else (job.error or "Check the logs for details.")
        )
        if view["unmapped_sets"]:
            body += f" · {len(view['unmapped_sets'])} unmapped"
        response.headers["HX-Toast"] = json.dumps(
            {"kind": kind, "title": title, "body": body}
        )
    return response
