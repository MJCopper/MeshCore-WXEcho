"""Live Traffic NSW pages and settings."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..rfs.councils import COUNCILS
from ..web.routes import render
from .feed import TYPES

router = APIRouter()


@router.get("/traffic", response_class=HTMLResponse)
async def traffic_page(request: Request):
    db = request.app.state.db
    poller = request.app.state.traffic_poller
    since = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(timespec="seconds")
    return render(request, "traffic.html", incidents=db.traffic_recent_items(since),
                  feed_status=poller.last_result, last_poll=poller.last_poll)


@router.get("/settings/traffic", response_class=HTMLResponse)
async def traffic_settings_page(request: Request):
    settings = request.app.state.db.all_settings()
    return render(request, "settings_traffic.html", s=settings, councils=COUNCILS, types=TYPES,
                  selected_councils=set(settings.get("traffic_councils", [])),
                  selected_types=set(settings.get("traffic_types", [])))


@router.post("/settings/traffic")
async def save_traffic_settings(request: Request,
                                traffic_enabled: str = Form(""),
                                traffic_poll_minutes: int = Form(10),
                                traffic_all_councils: str = Form(""),
                                traffic_councils: list[str] = Form(default=[]),
                                traffic_types: list[str] = Form(default=[])):
    db = request.app.state.db
    db.set_setting("traffic_enabled", bool(traffic_enabled))
    db.set_setting("traffic_poll_minutes", max(5, int(traffic_poll_minutes)))
    db.set_setting("traffic_all_councils", bool(traffic_all_councils))
    db.set_setting("traffic_councils", [x for x in COUNCILS if x in traffic_councils])
    db.set_setting("traffic_types", [x for x in TYPES if x in traffic_types])
    request.app.state.traffic_poller.poke()
    db.add_event("INFO", "Live Traffic NSW settings saved")
    return RedirectResponse("/settings/traffic", status_code=303)
