from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from card_catalog.config import settings as app_settings
from card_catalog.db.session import get_db
from card_catalog.services import settings as svc

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory=str(app_settings.templates_dir))


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db), saved: bool = False):
    values = svc.get_all(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "values": values,
            "specs": svc.SETTING_SPECS,
            "groups": svc.SETTING_GROUPS,
            "saved": saved,
            "active_nav": "settings",
        },
    )


@router.post("", response_class=HTMLResponse)
async def settings_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    updates: dict[str, str] = {}
    valid_keys = {s.key for s in svc.SETTING_SPECS}
    for key, value in form.multi_items():
        if key in valid_keys:
            updates[key] = str(value)
    svc.set_many(db, updates)

    values = svc.get_all(db)
    return templates.TemplateResponse(
        request,
        "partials/settings_form.html",
        {
            "values": values,
            "specs": svc.SETTING_SPECS,
            "groups": svc.SETTING_GROUPS,
            "saved": True,
        },
    )
