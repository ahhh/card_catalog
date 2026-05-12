"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from card_catalog.config import settings
from card_catalog.db.session import SessionLocal
from card_catalog.services import settings as settings_service


def _ensure_data_dirs() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "images").mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "bulk").mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_data_dirs()
    db = SessionLocal()
    try:
        settings_service.ensure_defaults(db)
    finally:
        db.close()
    yield


templates = Jinja2Templates(directory=str(settings.templates_dir))


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# Jinja globals available in every template
templates.env.globals["is_htmx"] = _is_htmx


def create_app() -> FastAPI:
    app = FastAPI(
        title="Card Catalog",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    app.mount(
        "/static", StaticFiles(directory=str(settings.static_dir), check_dir=False), name="static"
    )

    from card_catalog.routers import (
        archidekt,
        collection,
        dashboard,
        imports,
        prices,
        settings as settings_router,
    )

    app.include_router(dashboard.router)
    app.include_router(collection.router)
    app.include_router(imports.router)
    app.include_router(prices.router)
    app.include_router(archidekt.router)
    app.include_router(settings_router.router)

    @app.exception_handler(404)
    async def not_found(request: Request, _exc):
        return templates.TemplateResponse(
            request, "errors/404.html", {"request": request}, status_code=404
        )

    return app


app = create_app()
