"""Import routes: Manabox CSV upload, preview, commit, plus Scryfall bulk sync."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from card_catalog.clients import manabox_csv
from card_catalog.utils import utc_now
from card_catalog.config import settings as app_settings
from card_catalog.db.session import SessionLocal, get_db
from card_catalog.jobs.registry import registry
from card_catalog.jobs.runner import run_in_thread
from card_catalog.services import enrich_scryfall, import_manabox
from card_catalog.services import settings as settings_svc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/import", tags=["imports"])
# Jobs sub-router is nested under /import so the single mount in main.py picks it up.
# Final URLs: /import/jobs/scryfall/{id}, /import/jobs/import/{id}.
jobs_router = APIRouter(prefix="/jobs", tags=["imports"])

templates = Jinja2Templates(directory=str(app_settings.templates_dir))


def _toast_header(kind: str, title: str, body: str = "") -> dict[str, str]:
    return {"HX-Toast": json.dumps({"kind": kind, "title": title, "body": body})}


def _format_when(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    delta = utc_now() - dt
    if delta.total_seconds() < 60:
        return "just now"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    if days < 30:
        return f"{days} d ago"
    return dt.strftime("%b %d, %Y")


def _context(db: Session, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    owner_name = settings_svc.get(db, "collection_owner_name") or "Collector"
    stats = enrich_scryfall.last_sync_stats(db)
    ctx: dict[str, Any] = {
        "active_nav": "import",
        "owner_name": owner_name,
        "scryfall_card_count": stats["card_count"],
        "scryfall_last_fetched": stats["last_fetched_at"],
        "scryfall_last_fetched_pretty": _format_when(stats["last_fetched_at"]),
    }
    if extra:
        ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
# Page + initial dropzone
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def import_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "import.html", _context(db))


@router.get("/dropzone", response_class=HTMLResponse)
async def dropzone_fragment(request: Request, db: Session = Depends(get_db)):
    """Cancel/reset target — swap back to the dropzone."""
    return templates.TemplateResponse(
        request, "partials/import_dropzone.html", _context(db)
    )


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@router.post("/preview", response_class=HTMLResponse)
async def preview_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    raw = await file.read()
    filename = file.filename or "manabox.csv"

    default_condition = settings_svc.get(db, "default_condition") or "NM"
    default_finish = settings_svc.get(db, "default_finish") or "nonfoil"
    default_language = settings_svc.get(db, "default_language") or "en"

    try:
        rows, warnings = manabox_csv.parse(
            raw,
            default_condition=default_condition,
            default_finish=default_finish,
            default_language=default_language,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("manabox parse failed")
        return templates.TemplateResponse(
            request,
            "partials/import_preview.html",
            _context(
                db,
                {
                    "preview_id": None,
                    "verdicts": [],
                    "counts": {"insert": 0, "increment": 0, "unmatched": 0, "total": 0},
                    "warnings": [f"Couldn't parse the CSV: {exc}"],
                    "filename": filename,
                },
            ),
            headers=_toast_header("error", "Import failed", str(exc)),
        )

    preview_id, verdicts, _bundle = import_manabox.preview(
        db, rows, filename=filename, warnings=warnings
    )

    counts = {
        "insert": sum(1 for v in verdicts if v.action == "insert"),
        "increment": sum(1 for v in verdicts if v.action == "increment"),
        "unmatched": sum(1 for v in verdicts if v.action == "unmatched"),
        "total": len(verdicts),
    }
    qty_total = sum(int(v.row.quantity) for v in verdicts if v.action != "unmatched")

    return templates.TemplateResponse(
        request,
        "partials/import_preview.html",
        _context(
            db,
            {
                "preview_id": preview_id,
                "verdicts": verdicts,
                "counts": counts,
                "warnings": warnings,
                "filename": filename,
                "qty_total": qty_total,
            },
        ),
    )


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


@router.post("/commit", response_class=HTMLResponse)
async def commit_preview(
    request: Request,
    preview_id: str = Form(...),
    db: Session = Depends(get_db),
):
    bundle = import_manabox.get_preview(preview_id)
    if bundle is None:
        return templates.TemplateResponse(
            request,
            "partials/import_dropzone.html",
            _context(db, {"error": "That preview has expired. Upload the CSV again."}),
            headers=_toast_header(
                "error", "Preview expired", "Please re-upload your CSV."
            ),
            status_code=410,
        )

    actionable = sum(1 for v in bundle.verdicts if v.action in ("insert", "increment"))
    label = f"Importing {actionable} rows from {bundle.filename or 'CSV'}"

    def _target(job):
        # Fresh session — the request's session closes when this handler returns.
        worker_db = SessionLocal()
        try:
            registry.update(job.id, detail="Writing collection entries…", done=0)
            result = import_manabox.commit(worker_db, preview_id, consume=True)
            registry.update(
                job.id,
                done=result.rows_imported,
                detail=(
                    f"{result.inserted} new · {result.incremented} incremented"
                    + (f" · {result.rows_unmatched} unmatched" if result.rows_unmatched else "")
                ),
            )
            # Stash result so the terminal-state poll can render a summary.
            persisted_job = registry.get(job.id)
            if persisted_job is not None:
                persisted_job.extras.update(
                    {
                        "inserted": result.inserted,
                        "incremented": result.incremented,
                        "unmatched": result.rows_unmatched,
                        "imported": result.rows_imported,
                        "total": result.rows_total,
                        "import_run_id": result.import_run_id,
                    }
                )
        finally:
            worker_db.close()

    job = run_in_thread(
        kind="import_commit",
        label=label,
        target=_target,
        total=actionable,
        singleton=False,
    )

    return templates.TemplateResponse(
        request,
        "partials/import_preview.html",
        _context(
            db,
            {
                "preview_id": None,
                "verdicts": [],
                "counts": {"insert": 0, "increment": 0, "unmatched": 0, "total": 0},
                "job": job,
                "poll_url": f"/import/jobs/import/{job.id}",
                "extras": {},
                "committing": True,
            },
        ),
    )


# ---------------------------------------------------------------------------
# Scryfall bulk sync trigger + poll
# ---------------------------------------------------------------------------


@router.post("/scryfall-sync", response_class=HTMLResponse)
async def trigger_scryfall_sync(request: Request, db: Session = Depends(get_db)):
    def _target(job):
        worker_db = SessionLocal()
        try:
            enrich_scryfall.bulk_sync(worker_db, job)
        finally:
            worker_db.close()

    job = run_in_thread(
        kind="scryfall_bulk_sync",
        label="Syncing Scryfall card cache",
        target=_target,
        total=0,
        singleton=True,
    )
    return templates.TemplateResponse(
        request,
        "partials/job_status.html",
        {"job": job, "poll_url": f"/import/jobs/scryfall/{job.id}"},
        headers=_toast_header(
            "info", "Scryfall sync started", "We'll keep this page updated as it runs."
        ),
    )


# ---------------------------------------------------------------------------
# Job poll endpoints (live at /jobs/...)
# ---------------------------------------------------------------------------


@jobs_router.get("/scryfall/{job_id}", response_class=HTMLResponse)
async def poll_scryfall_job(request: Request, job_id: str, db: Session = Depends(get_db)):
    job = registry.get(job_id)
    if job is None:
        return HTMLResponse("<div class='subtle'>Job not found.</div>", status_code=404)
    headers = {}
    if job.is_terminal:
        if job.status.value == "completed":
            headers = _toast_header(
                "success", "Scryfall sync complete", job.detail or "Card cache is up to date."
            )
        else:
            headers = _toast_header("error", "Scryfall sync failed", job.error or "Unknown error.")
    return templates.TemplateResponse(
        request,
        "partials/job_status.html",
        {"job": job, "poll_url": f"/import/jobs/scryfall/{job.id}"},
        headers=headers,
    )


@jobs_router.get("/import/{job_id}", response_class=HTMLResponse)
async def poll_import_job(request: Request, job_id: str, db: Session = Depends(get_db)):
    job = registry.get(job_id)
    if job is None:
        return HTMLResponse("<div class='subtle'>Job not found.</div>", status_code=404)
    extras = job.extras or {}
    headers = {}
    if job.is_terminal:
        if job.status.value == "completed":
            body = (
                f"{extras.get('imported', 0)} cards landed in your collection."
                if extras
                else "Import finished."
            )
            headers = _toast_header("success", "Import complete", body)
        else:
            headers = _toast_header("error", "Import failed", job.error or "Unknown error.")

    return templates.TemplateResponse(
        request,
        "partials/import_job.html",
        {
            "job": job,
            "poll_url": f"/import/jobs/import/{job.id}",
            "extras": extras,
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Wire the sub-router. We export a single `router` (mounted in main.py); fold the
# jobs_router into it so a single include keeps the existing main.py intact.
# ---------------------------------------------------------------------------

# main.py includes `imports.router`. We want `/jobs/scryfall/...` and
# `/jobs/import/...` paths too. Use FastAPI's include_router on the page router
# with the jobs sub-router so the caller only mounts one thing.
router.include_router(jobs_router)


# Sub-router gets attached at the app level via include in main.py; we expose it on
# the module so the FastAPI app can pick it up if it ever wants to mount it separately.
__all__ = ["router", "jobs_router"]
