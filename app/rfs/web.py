"""NSW RFS settings and history pages, separate from BOM routes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, Request, Query
import json
from fastapi.responses import HTMLResponse, RedirectResponse

from ..web.routes import render
from .councils import COUNCILS
from .feed import LEVELS
from ..presentation import freshness

router = APIRouter()


@router.get("/rfs", response_class=HTMLResponse)
async def rfs_page(request: Request, page: int = Query(1, ge=1)):
    db = request.app.state.db
    s = db.all_settings()
    poller = request.app.state.rfs_poller
    rows, pagination = db.current_listing("rfs", page)
    last_success = db.get_setting("rfs_last_successful_poll", getattr(poller, "last_successful_poll", ""))
    return render(request, "rfs.html", s=s, councils=COUNCILS, levels=LEVELS,
                  selected_councils=set(s.get("rfs_councils", [])),
                  selected_levels=set(s.get("rfs_levels", [])),
                  incidents=[dict(row) | {"data": json.loads(row["normalized_data"])} for row in rows],
                  pagination=pagination, last_success=last_success,
                  snapshot_freshness=freshness(last_success, s.get("rfs_poll_minutes", 10), s.get("rfs_enabled", False)),
                  rfs_status=poller.last_result, rfs_last_poll=poller.last_poll)


@router.get("/settings/rfs", response_class=HTMLResponse)
async def rfs_settings_page(request: Request):
    db = request.app.state.db
    s = db.all_settings()
    return render(request, "settings_rfs.html", s=s, councils=COUNCILS, levels=LEVELS,
                  selected_councils=set(s.get("rfs_councils", [])),
                  selected_levels=set(s.get("rfs_levels", [])))


@router.post("/settings/rfs")
async def save_rfs_settings(request: Request,
                            rfs_enabled: str = Form(""),
                            rfs_poll_minutes: int = Form(10),
                            rfs_all_councils: str = Form(""),
                            rfs_councils: list[str] = Form(default=[]),
                            rfs_levels: list[str] = Form(default=[])):
    db = request.app.state.db
    db.set_setting("rfs_enabled", bool(rfs_enabled))
    db.set_setting("rfs_poll_minutes", max(5, int(rfs_poll_minutes)))
    db.set_setting("rfs_all_councils", bool(rfs_all_councils))
    if not rfs_all_councils or rfs_councils:
        db.set_setting("rfs_councils", [name for name in COUNCILS if name in rfs_councils])
    db.set_setting("rfs_levels", [x for x in LEVELS if x in rfs_levels])
    request.app.state.rfs_poller.poke()
    db.add_event("INFO", "NSW RFS settings saved")
    return RedirectResponse("/settings/rfs", status_code=303)


@router.post("/rfs/settings")
async def save_rfs_settings_legacy(request: Request,
                                   rfs_enabled: str = Form(""),
                                   rfs_poll_minutes: int = Form(10),
                                   rfs_all_councils: str = Form(""),
                                   rfs_councils: list[str] = Form(default=[]),
                                   rfs_levels: list[str] = Form(default=[])):
    return await save_rfs_settings(request, rfs_enabled, rfs_poll_minutes,
                                   rfs_all_councils, rfs_councils, rfs_levels)
