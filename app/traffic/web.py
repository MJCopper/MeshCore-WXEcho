"""Live Traffic NSW pages and settings."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from fastapi import APIRouter, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from ..rfs.councils import COUNCILS
from ..web.routes import render
from .feed import TYPES
from .schedule import ClosurePeriod, window_state
from ..presentation import freshness
import time

router = APIRouter()


@router.get("/traffic", response_class=HTMLResponse)
async def traffic_page(request: Request, page: int = Query(1, ge=1)):
    db = request.app.state.db
    poller = request.app.state.traffic_poller
    rows, pagination = db.current_listing("traffic", page)
    incidents = [dict(row) | {"data": json.loads(row["normalized_data"])}
                 for row in rows]
    s = db.all_settings()
    for item in incidents:
        data = item["data"]
        item["schedule"] = [ClosurePeriod(**p).describe() for p in data.get("periods", [])]
        item["freshness"] = freshness(item["last_seen"], s.get("traffic_poll_minutes", 10), s.get("traffic_enabled", False))
        item["current_now"] = bool(item["active"] and not data.get("ended") and
                                    (data.get("start") is None or data["start"] <= time.time()) and
                                    (data.get("end") is None or data["end"] > time.time()))
        item["closure_state"] = (window_state(tuple(ClosurePeriod(**p) for p in data.get("periods", [])), time.time())[1]
                                 if item["current_now"] else "Notice is not currently active; closure status is unconfirmed")
    feeds = []
    saved_feeds = {row["feed"]: dict(row) for row in db.traffic_feed_status()}
    for name in TYPES:
        record = saved_feeds.get(name, {"feed": name, "last_success": "", "published": "", "error": ""})
        requested = name in s.get("traffic_types", []) and not (name == "fire" and s.get("rfs_enabled"))
        record["freshness"] = (freshness(record["last_success"], s.get("traffic_poll_minutes", 10), s.get("traffic_enabled", False))
                               if requested else "Not requested with current settings")
        feeds.append(record)
    return render(request, "traffic.html", incidents=incidents,
                  feed_status=poller.last_result, last_poll=poller.last_poll,
                  pagination=pagination, feeds=feeds)


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
    if not traffic_all_councils or traffic_councils:
        db.set_setting("traffic_councils", [name for name in COUNCILS if name in traffic_councils])
    db.set_setting("traffic_types", [x for x in TYPES if x in traffic_types])
    request.app.state.traffic_poller.poke()
    db.add_event("INFO", "Live Traffic NSW settings saved")
    return RedirectResponse("/settings/traffic", status_code=303)
