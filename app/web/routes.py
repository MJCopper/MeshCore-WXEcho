"""Web UI routes (server-rendered templates + htmx partials)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import MAX_PAYLOAD_BYTES, MIN_POLL_MINUTES, polling_seconds
from ..rfs.councils import COUNCILS
from ..meshcore_discovery import list_usb_serial_devices


def _template_dir() -> Path:
    """Locate the templates both in a normal run and inside a PyInstaller bundle."""
    if getattr(sys, "frozen", False):  # packaged (Windows .exe / onedir)
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "app" / "web" / "templates"
    return Path(__file__).parent / "templates"


router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(_template_dir()))

from ..formatter import fmt_local
from ..history import history_sources, get_history_source, history_source_label
TEMPLATES.env.filters["localtime"] = fmt_local


# History/dashboard status labels describe the FILTER decision, not transmission
# (whether a message actually went out lives in the Transmit Log). So "sent"
# becomes "pass" (the alert passed your rules); the rest already describe the
# alert, not a send.
DISP_LABELS = {
    "sent": "pass",
    "filtered": "filtered",
    "update": "update",
    "cancelled": "cancelled",
    "duplicate": "duplicate",
    "excluded-alert-level": "excluded: alert level",
    "excluded-council": "excluded: council",
    "absent-from-feed": "absent from feed",
    "baseline": "existing item",
    "deferred-queue": "waiting for send queue",
    "excluded-hazard-type": "excluded: hazard type",
    "excluded-rfs-fire-coverage": "excluded: RFS fire coverage",
    "excluded-ended-or-not-yet-active": "ended or future",
}

TX_STATUS_LABELS = {
    "queued": "queued",
    "deferred": "waiting for send queue",
    "success": "locally transmitted",
    "failed": "failed",
    "dry-run": "dry-run",
    "interrupted": "interrupted",
}


def render(request: Request, name: str, **ctx):
    try:
        tz = request.app.state.db.get_setting("display_timezone", "")
    except Exception:
        tz = ""   # fall back to this machine's local time
    return TEMPLATES.TemplateResponse(
        request, name,
        {"max_bytes": getattr(_tx(request), "message_budget", MAX_PAYLOAD_BYTES), "tz": tz, "disp_label": DISP_LABELS,
         "tx_label": TX_STATUS_LABELS, "version": __version__, **ctx},
    )


def _db(request: Request):
    return request.app.state.db


def _tx(request: Request):
    return request.app.state.tx


def _poller(request: Request):
    return request.app.state.poller


def _status_flag(request: Request, name: str) -> bool:
    return any(s["connected"] for s in _tx(request).status() if s["name"] == name)


def _status_ctx(request: Request) -> dict:
    db, tx, poller = _db(request), _tx(request), _poller(request)
    return {
        "last_poll_time": poller.status.last_poll_time,
        "last_poll_result": poller.status.last_poll_result,
        "port": tx.port or "(none)",
        "connected": tx.connected,
        "tx_error": tx.last_error,
        "dry_run": bool(db.get_setting("dry_run", True)),
        "queue_depth": tx.queue_depth,
        "uptime": poller.status.uptime_seconds,
        "events": db.recent_events(10),
    }



def _spark(counts):
    if not counts or max(counts) == 0:
        return "", ""
    n, mx, W, H, pad = len(counts), max(counts), 720, 80, 8
    pts = []
    for i, c in enumerate(counts):
        x = 0 if n == 1 else i * (W / (n - 1))
        y = H - pad - (c / mx) * (H - 2 * pad)
        pts.append((x, y))
    line = "M" + " L".join("%.0f,%.0f" % (x, y) for x, y in pts)
    return line, line + " L%d,%d L0,%d Z" % (W, H, H)


def _dash_ctx(request) -> dict:
    db, tx, poller = _db(request), _tx(request), _poller(request)
    tz = db.get_setting("display_timezone", "")
    port = tx.port or ""
    device = "Heltec V3" if ("CP210" in port or "Silicon_Labs" in port) else (port.split("/")[-1] if port else "(none)")
    up = poller.status.uptime_seconds
    dd, rem = divmod(up, 86400); hh, rem = divmod(rem, 3600); mm = rem // 60
    uptime_str = ("%dd %dh" % (dd, hh)) if dd else ("%dh %dm" % (hh, mm))
    now = datetime.datetime.now(datetime.timezone.utc)
    def _age(ts):
        try:
            return (now - datetime.datetime.fromisoformat(ts)).total_seconds() / 86400.0
        except Exception:
            return 999
    if hasattr(db, "successful_service_activity"):
        activity = db.successful_service_activity(now)
        sent_7d = sum(activity.values())
        sent_today = activity.get(0, 0)
        buckets = [activity.get(6 - index, 0) for index in range(7)]
    else:
        rows = (db.query_service_history(limit=1000) if hasattr(db, "query_service_history")
                else db.query_history(limit=1000))
        sent = [r for r in rows if r["transmit_status"] == "success"]
        sent_7d = sum(1 for r in sent if _age(r["ts"]) < 7)
        sent_today = sum(1 for r in sent if _age(r["ts"]) < 1)
        buckets = [0] * 7
        for r in sent:
            age = _age(r["ts"])
            if 0 <= age < 7:
                buckets[6 - int(age)] += 1
    spark_line, spark_fill = _spark(buckets)
    recent_rows = []
    seen_items = set()
    candidates = (db.query_service_history(transmit_status="success", limit=2000)
                  if hasattr(db, "query_service_history") else db.query_history(limit=200))
    for r in sorted(candidates, key=lambda row: (row.get("transmitted_at") or row["ts"],
                                                 row["id"] if "id" in row.keys() else 0),
                    reverse=True):
        if r["transmit_status"] != "success":
            continue
        source = r["source"] if "source" in r.keys() else "bom"
        external_id = r["external_id"] if "external_id" in r.keys() else r.get("alert_id", "")
        identity = (source, external_id)
        if external_id and identity in seen_items:
            continue
        if external_id:
            seen_items.add(identity)
        recent_rows.append(r)
        if len(recent_rows) == 6:
            break
    recent = [{
        "source": history_source_label(r["source"] if "source" in r.keys() else "bom"),
        "event": r["title"] if "title" in r.keys() else r["event"],
        "area": r["area"], "disposition": r["disposition"],
        "transmit_status": r["transmit_status"], "detail": r["detail"],
        "text": r["transmitted_text"],
        "when": fmt_local(r.get("transmitted_at") or r["ts"], tz),
    } for r in recent_rows]
    ltx = db.query_transmit_log(limit=1)
    last_tx = "-"
    if ltx:
        # ASCII only: this string is auto-escaped through {{ }}, so a non-ASCII
        # separator could mojibake depending on charset. Keep it plain.
        last_tx = ("OK - " if ltx[0]["success"] else "failed - ") + fmt_local(ltx[0]["ts"], tz)

    # Each enabled source has its own feed health and interval.
    st = poller.status
    dry = bool(db.get_setting("dry_run", True))
    def _age_s(ts):
        try:
            return (now - datetime.datetime.fromisoformat(ts)).total_seconds()
        except Exception:
            return None

    rfs_poller = getattr(request.app.state, "rfs_poller", None)
    traffic_poller = getattr(request.app.state, "traffic_poller", None)
    service_specs = (
        ("BOM", "bom", "/bom", "/settings/bom", True, 5,
         st.last_poll_time, st.last_poll_success_time, st.last_poll_result),
        ("NSW RFS", "rfs", "/rfs", "/settings/rfs", False, 10,
         getattr(rfs_poller, "last_poll", ""),
         getattr(rfs_poller, "last_successful_poll", ""),
         getattr(rfs_poller, "last_result", "not polled")),
        ("Live Traffic NSW", "traffic", "/traffic", "/settings/traffic", False, 10,
         getattr(traffic_poller, "last_poll", ""),
         getattr(traffic_poller, "last_successful_poll", ""),
         getattr(traffic_poller, "last_result", "not polled")),
    )
    services = []
    problems = []
    for label, key, url, settings_url, enabled_default, interval_default, last_poll, last_success, result in service_specs:
        enabled = bool(db.get_setting(f"{key}_enabled", enabled_default))
        minutes = polling_seconds(db.get_setting(f"{key}_poll_minutes", interval_default),
                                  interval_default) // 60
        success_age = _age_s(last_success)
        stale_after = max(minutes * 180, 360)
        state = "disabled"
        if enabled:
            state = "ok"
            if result.startswith("select councils"):
                state = "needs setup"
                problems.append(("warn", f"{label}: {result}."))
            elif result.startswith("error"):
                state = "error"
                problems.append(("warn", f"{label} poll failed: {result[7:][:80]}"))
            elif result.startswith("partial"):
                state = "partial"
                problems.append(("warn", f"{label} poll partial: {result[9:][:80]}"))
            if success_age is None:
                if state == "ok":
                    state = "waiting"
                    problems.append(("warn", f"No successful {label} poll yet."))
            elif success_age > stale_after:
                state = "stale"
                problems.append(("critical", f"{label} last successful poll was {int(success_age // 60)} min ago."))
        services.append({
            "key": key, "label": label, "url": url, "settings_url": settings_url,
            "enabled": enabled, "interval": minutes, "result": result,
            "state": state, "last_success": fmt_local(last_success, tz) if last_success else "Never",
            "last_poll": fmt_local(last_poll, tz) if last_poll else "Never",
        })
    bom_enabled = services[0]["enabled"]
    enabled_services = [service for service in services if service["enabled"]]
    latest_poll = max((service[6] for service in service_specs if service[6]), default="")
    # 2. Radios: any enabled radio offline means alerts may not go out.
    radios = tx.status()
    on = [r for r in radios if r["enabled"]]
    off = [r for r in on if not r["connected"]]
    if on and len(off) == len(on):
        problems.append(("critical", "All radios offline. Alerts cannot be broadcast."))
    elif off:
        problems.append(("warn", "%s offline." % ", ".join(r["label"] for r in off)))
    if not on:
        problems.append(("critical", "No radios enabled. Nothing will broadcast."))
    # 3. A recent alert that failed on every radio (verified-transmit feedback).
    bf_age = _age_s(st.last_broadcast_failure)
    if bf_age is not None and bf_age < 1800:
        problems.append(("critical",
            "A broadcast FAILED %d min ago and is being retried: %s"
            % (bf_age // 60, (st.last_broadcast_failure_text or "")[:60])))
    # 4. Backed-up queue.
    if tx.queue_depth > 5:
        problems.append(("warn", "Transmit queue backed up (%d waiting)." % tx.queue_depth))
    if hasattr(db, "recent_delivery_failures"):
        since = (now - datetime.timedelta(minutes=30)).isoformat(timespec="seconds")
        for failed in db.recent_delivery_failures(since):
            label = history_source_label(failed["source"])
            problems.append(("warn", f"{label} local transmission {failed['transmit_status']}: "
                                     f"{failed['title'][:55]}"))
    # 5. System clock skew vs BOM: makes alert "until" times wrong.
    skew = getattr(st, "clock_skew_seconds", None)
    if skew is not None and abs(skew) > 120:
        problems.append(("warn",
            "System clock is off by %d s vs BOM. Alert times may be wrong; check the VM clock."
            % int(abs(skew))))
    health_level = ("critical" if any(l == "critical" for l, _ in problems)
                    else "warn" if problems else "ok")

    return {
        "health_level": health_level,
        "health_problems": [m for _, m in problems],
        "health_paused": dry,
        "dry_run": bool(db.get_setting("dry_run", True)),
        "connected": tx.connected, "device": device, "tx_error": tx.last_error,
        "channel_index": int(db.get_setting("meshcore_channel", 0)),
        "bom_enabled": bom_enabled, "services": services,
        "enabled_services": enabled_services,
        "queue_depth": tx.queue_depth,
        "last_poll_local": fmt_local(latest_poll, tz) if latest_poll else "-",
        "uptime_str": uptime_str, "sent_7d": sent_7d, "sent_today": sent_today,
        "spark_line": spark_line, "spark_fill": spark_fill,
        "recent": recent, "last_tx": last_tx,
        "transports": tx.status(),
    }


# ---- liveness probe ----------------------------------------------------
@router.get("/healthz", response_class=PlainTextResponse)
async def healthz(request: Request):
    # If this responds, the event loop is servicing requests (i.e. not wedged).
    # Used by the Docker HEALTHCHECK; deliberately does no DB/radio work so it
    # is a pure liveness signal, not a readiness check.
    return PlainTextResponse("ok")


# ---- dashboard ---------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return render(request, "dashboard.html", **_dash_ctx(request))


@router.get("/partials/status", response_class=HTMLResponse)
async def status_partial(request: Request):
    return render(request, "_dash_top.html", **_dash_ctx(request))


@router.get("/partials/dashboard", response_class=HTMLResponse)
async def dashboard_cols_partial(request: Request):
    # Recent successful notices and radio status, for live polling.
    return render(request, "_dash_cols.html", **_dash_ctx(request))


@router.post("/dry-run/toggle", response_class=HTMLResponse)
async def toggle_dry_run(request: Request):
    db = _db(request)
    db.set_setting("dry_run", not bool(db.get_setting("dry_run", True)))
    db.add_event("INFO", f"dry-run set to {db.get_setting('dry_run')}")
    return render(request, "_dash_top.html", **_dash_ctx(request))


# ---- shared history ----------------------------------------------------
def _superseded_history_ids(rows):
    seen, superseded = set(), set()
    for row in rows:
        source = row.get("source", "bom")
        external_id = row.get("external_id", row.get("alert_id", ""))
        key = (source, external_id)
        if external_id and key in seen:
            superseded.add(row["id"])
        elif external_id:
            seen.add(key)
    return superseded


def _history_context(request, source="", disposition="", transmit_status="",
                     date_from="", date_to="", facet=""):
    db = _db(request)
    selected = get_history_source(source)
    source = source if selected else ""
    kwargs = dict(source=source or None, disposition=disposition or None,
                  transmit_status=transmit_status or None,
                  date_from=date_from or None, date_to=date_to or None)
    if selected and selected.facet_key and facet:
        kwargs.update(facet_key=selected.facet_key, facet_value=facet)
    rows = db.query_service_history(**kwargs)
    for row in rows:
        row["source_label"] = history_source_label(row["source"])
    return dict(rows=rows, superseded_ids=_superseded_history_ids(rows),
                source=source, sources=history_sources(), selected_source=selected,
                disposition=disposition, transmit_status=transmit_status,
                date_from=date_from, date_to=date_to, facet=facet,
                dispositions=["sent", "filtered", "update", "cancelled"],
                transmit_statuses=["queued", "deferred", "success", "failed", "interrupted", "dry-run"])


@router.get("/history", response_class=HTMLResponse)
async def history(request: Request, source: str = "", disposition: str = "",
                  transmit_status: str = "", date_from: str = "", date_to: str = "",
                  facet: str = ""):
    return render(request, "history.html", **_history_context(
        request, source, disposition, transmit_status, date_from, date_to, facet))


@router.get("/partials/history", response_class=HTMLResponse)
async def history_partial(request: Request, source: str = "", disposition: str = "",
                          transmit_status: str = "", date_from: str = "", date_to: str = "",
                          facet: str = ""):
    return render(request, "_history_rows.html", **_history_context(
        request, source, disposition, transmit_status, date_from, date_to, facet))


# ---- transmit log ------------------------------------------------------
@router.get("/transmit-log", response_class=HTMLResponse)
async def transmit_log(request: Request):
    rows = _db(request).query_transmit_log()
    return render(request, "transmit_log.html", rows=rows)


@router.get("/partials/transmit-log", response_class=HTMLResponse)
async def transmit_log_partial(request: Request):
    return render(request, "_transmit_rows.html", rows=_db(request).query_transmit_log())


@router.post("/transmit-log/resend/{entry_id}", response_class=HTMLResponse)
async def resend_log(request: Request, entry_id: int):
    db, tx = _db(request), _tx(request)
    row = db.get_transmit_log(entry_id)
    if row is not None and row["transport"]:
        await tx.resend(row["transport"], row["text"] or "", int(row["channel"]))
    return render(request, "_transmit_rows.html", rows=db.query_transmit_log())



_TIMEZONES = [
    ("Australia/Sydney", "Sydney / Melbourne / Canberra"),
    ("Australia/Adelaide", "Adelaide"),
    ("Australia/Brisbane", "Brisbane"),
    ("Australia/Darwin", "Darwin"),
    ("Australia/Hobart", "Hobart"),
    ("Australia/Perth", "Perth"),
    ("UTC", "UTC"),
]

_EVENT_GROUPS = {
    "Warning products": [
        "Severe Thunderstorm Warning", "Severe Weather Warning", "Flood Warning",
        "Fire Weather Warning", "Heatwave Warning", "Tropical Cyclone Warning",
        "Tsunami Warning", "Marine Wind Warning", "Hazardous Surf Warning",
        "Damaging Surf Warning", "Coastal Hazard Warning", "Frost Warning",
        "Warning to Sheep Graziers",
    ],
    "Watches and advice": ["Flood Watch", "Tropical Cyclone Advice"],
    "Specialised alerts": ["Road Weather Alert", "Bush Walkers Weather Alert"],
}

_KNOWN_EVENTS = {event for group in _EVENT_GROUPS.values() for event in group}


# ---- settings ----------------------------------------------------------
@router.get("/settings", response_class=HTMLResponse)
async def settings_home(request: Request):
    return render(request, "settings_home.html")


@router.get("/settings/general", response_class=HTMLResponse)
async def general_settings_page(request: Request):
    db = _db(request)
    tz = db.get_setting("display_timezone", "Australia/Sydney")
    return render(request, "settings_general.html", timezones=_TIMEZONES,
                  tz_current=tz, dry_run=bool(db.get_setting("dry_run", True)))


@router.post("/settings/general")
async def save_general_settings(request: Request,
                                display_timezone: str = Form("Australia/Sydney"),
                                dry_run: str = Form("")):
    db = _db(request)
    allowed = {value for value, _ in _TIMEZONES}
    db.set_setting("display_timezone", display_timezone if display_timezone in allowed else "Australia/Sydney")
    db.set_setting("dry_run", bool(dry_run))
    db.add_event("INFO", "general settings saved")
    return RedirectResponse("/settings/general", status_code=303)


@router.get("/settings/legacy", response_class=HTMLResponse)
async def settings_page(request: Request):
    db = _db(request)
    s = db.all_settings()
    mc_connected = _status_flag(request, "meshcore")
    return render(
        request, "settings.html", s=s, min_interval=MIN_POLL_MINUTES, ports=list_usb_serial_devices(),
        timezones=_TIMEZONES, tz_current=(s.get("display_timezone", "") or ""),
        tz_known={v for v, _ in _TIMEZONES},
        event_groups=_EVENT_GROUPS, councils=COUNCILS,
        selected_councils=set(s.get("bom_councils", [])),
        selected_events=set(s.get("filter_include_exact", []) or []),
        all_warnings=bool(s.get("filter_include_suffix", []) or []), err="",
        mc_conn=s.get("meshcore_conn", "serial") or "serial",
        mc_port=s.get("meshcore_port", "") or "",
        mc_host=s.get("meshcore_host", "") or "",
        **_channel_ctx(
            db,
            _tx(request),
            "meshcore",
            error="",
            connected=mc_connected,
        ),
    )


@router.get("/bom", response_class=HTMLResponse)
async def bom_page(request: Request):
    db = _db(request)
    regions = ["NSW"]
    poll_status = _poller(request).status
    return render(
        request, "bom.html", items=[dict(row) | {"matched_councils": json.loads(row["matched_councils"] or "[]")}
                                    for row in db.bom_current_items(regions)],
        snapshots={row["region"]: row["fetched_at"] for row in db.bom_snapshot_regions(regions)},
        regions=regions, bom_enabled=bool(db.get_setting("bom_enabled", True)),
        bom_status=poll_status.last_poll_result,
        bom_last_poll=poll_status.last_poll_time,
    )


@router.get("/settings/bom", response_class=HTMLResponse)
async def bom_settings_page(request: Request):
    legacy = await settings_page(request)
    return render(request, "settings_bom.html", **{k: v for k, v in legacy.context.items()
                                                   if k not in ("request", "max_bytes", "tz", "disp_label", "tx_label", "version")})


@router.post("/settings/bom")
async def save_bom_settings(request: Request, poll_interval: int = Form(...),
                            bom_enabled: str = Form(""),
                            bom_all_councils: str = Form(""),
                            bom_councils: list[str] = Form(default=[]),
                            bom_include_unknown_councils: str = Form(""),
                            bom_districts: str = Form(""), events: list[str] = Form(default=[]),
                            all_warnings: str = Form(""), warning_choices_submitted: str = Form("")):
    db = _db(request)
    db.set_setting("bom_enabled", bool(bom_enabled))
    db.set_setting("bom_all_councils", bool(bom_all_councils))
    if not bom_all_councils or bom_councils:
        db.set_setting("bom_councils", [name for name in COUNCILS if name in bom_councils])
    db.set_setting("bom_include_unknown_councils", bool(bom_include_unknown_councils))
    db.set_setting("bom_districts", [d.strip() for d in bom_districts.replace(",", "\n").splitlines() if d.strip()])
    minutes = max(MIN_POLL_MINUTES, int(poll_interval))
    db.set_setting("bom_poll_minutes", minutes)
    db.set_setting("poll_interval", minutes * 60)
    selected = [e for e in events if e in _KNOWN_EVENTS]
    if all_warnings and warning_choices_submitted != "1":
        old = [e for e in db.get_setting("filter_include_exact", []) if e in _EVENT_GROUPS["Warning products"]]
        selected = old + [e for e in selected if e not in _EVENT_GROUPS["Warning products"]]
    db.set_setting("filter_include_exact", list(dict.fromkeys(selected)))
    db.set_setting("filter_include_suffix", ["Warning"] if all_warnings else [])
    db.set_setting("filter_exclude_exact", [])
    _poller(request).poke()
    db.add_event("INFO", "BOM settings saved")
    return RedirectResponse("/settings/bom", status_code=303)


@router.get("/settings/meshcore", response_class=HTMLResponse)
async def meshcore_connection_page(request: Request):
    legacy = await settings_page(request)
    return render(request, "settings_meshcore_connection.html", **{k: v for k, v in legacy.context.items()
                  if k not in ("request", "max_bytes", "tz", "disp_label", "tx_label", "version")})


@router.post("/settings/meshcore")
async def save_meshcore_connection(request: Request, meshcore_conn: str = Form("serial"),
                                   meshcore_port: str = Form(""), meshcore_host: str = Form(""),
                                   meshcore_channel: int = Form(0),
                                   meshcore_test_channel: int = Form(1)):
    db = _db(request)
    db.set_setting("meshcore_enabled", True)
    db.set_setting("meshcore_conn", meshcore_conn if meshcore_conn in ("serial", "tcp") else "serial")
    db.set_setting("meshcore_port", meshcore_port.strip())
    db.set_setting("meshcore_host", meshcore_host.strip())
    db.set_setting("meshcore_channel", meshcore_channel)
    db.set_setting("meshcore_test_channel", meshcore_test_channel)
    await _tx(request).reconfigure()
    db.add_event("INFO", "MeshCore connection saved")
    return RedirectResponse("/settings/meshcore", status_code=303)


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    poll_interval: int = Form(...),
    bom_enabled: str = Form(""),
    display_timezone: str = Form("Australia/Sydney"),
    bom_all_councils: str = Form(""),
    bom_councils: list[str] = Form(default=[]),
    bom_include_unknown_councils: str = Form(""),
    bom_districts: str = Form(""),
    events: list[str] = Form(default=[]),
    all_warnings: str = Form(""),
    warning_choices_submitted: str = Form(""),
    meshcore_conn: str = Form("serial"),
    meshcore_port: str = Form(""),
    meshcore_host: str = Form(""),
    meshcore_channel: int = Form(0),
    meshcore_test_channel: int = Form(1),
):
    def _rep(v):
        try:
            return max(1, min(5, int(v)))
        except (TypeError, ValueError):
            return 2
    db, tx, poller = _db(request), _tx(request), _poller(request)
    interval = max(MIN_POLL_MINUTES, int(poll_interval))

    db.set_setting("bom_enabled", bool(bom_enabled))
    db.set_setting("bom_all_councils", bool(bom_all_councils))
    if not bom_all_councils or bom_councils:
        db.set_setting("bom_councils", [name for name in COUNCILS if name in bom_councils])
    db.set_setting("bom_include_unknown_councils", bool(bom_include_unknown_councils))
    db.set_setting("bom_districts", [d.strip() for d in bom_districts.replace(",", "\n").splitlines() if d.strip()])
    db.set_setting("bom_poll_minutes", interval)
    db.set_setting("poll_interval", interval * 60)
    db.set_setting("display_timezone", display_timezone.strip())
    selected = [e for e in events if e in _KNOWN_EVENTS]
    if all_warnings and warning_choices_submitted != "1":
        # Disabled warning checkboxes are omitted by browsers without JavaScript.
        # Keep saved warning choices while still accepting watches and alerts.
        saved_warnings = [e for e in db.get_setting("filter_include_exact", [])
                          if e in _EVENT_GROUPS["Warning products"]]
        selected = saved_warnings + [e for e in selected
                                     if e not in _EVENT_GROUPS["Warning products"]]
    db.set_setting("filter_include_exact", list(dict.fromkeys(selected)))
    db.set_setting("filter_include_suffix", ["Warning"] if all_warnings else [])
    db.set_setting("filter_exclude_exact", [])

    db.set_setting("meshcore_enabled", True)
    db.set_setting("meshcore_conn", (meshcore_conn or "serial").strip())
    db.set_setting("meshcore_port", meshcore_port.strip())
    db.set_setting("meshcore_host", meshcore_host.strip())
    db.set_setting("meshcore_channel", int(meshcore_channel))
    db.set_setting("meshcore_test_channel", int(meshcore_test_channel))

    # Rebuild transports from the new settings and (re)connect the enabled ones.
    await tx.reconfigure()

    poller.poke()
    db.add_event("INFO", "settings saved")
    return RedirectResponse("/settings", status_code=303)


_RADIO_FIELDS = {
    "meshcore":   ("meshcore_conn", "meshcore_port", "meshcore_host",
                   "meshcore_channel", "meshcore_test_channel"),
}


def _channel_target(conn: str, port: str, host: str) -> dict:
    return {"conn": conn, "target": (host if conn == "tcp" else port).strip()}


def _channel_ctx(db, tx, name, channels=None, model=None, error="", connected=None,
                 conn=None, port=None, host=None, live_val=None, test_val=None):
    conn_f, port_f, host_f, live_f, test_f = _RADIO_FIELDS[name]
    conn = conn if conn is not None else (db.get_setting(conn_f, "serial") or "serial")
    port = port if port is not None else (db.get_setting(port_f, "") or "")
    host = host if host is not None else (db.get_setting(host_f, "") or "")
    cached_target = db.get_setting(name + "_channels_target", None)
    target = _channel_target(conn, port, host)
    if channels is None:
        channels = (db.get_setting(name + "_channels", []) or []) if cached_target == target else []
    if model is None:
        model = (db.get_setting(name + "_model", "") or "") if cached_target == target else ""
    if connected is None:
        connected = any(s["connected"] for s in tx.status() if s["name"] == name)
    return dict(
        radio=name, channels=channels, error=error, connected=connected, model=model,
        live_field=live_f, test_field=test_f,
        live_val=int(live_val if live_val is not None else (db.get_setting(live_f, 0) or 0)),
        test_val=int(test_val if test_val is not None else (db.get_setting(test_f, 1) or 1)),
    )


@router.post("/settings/channels/{name}", response_class=HTMLResponse)
async def load_channels(request: Request, name: str):
    if name not in _RADIO_FIELDS:
        return PlainTextResponse("unknown radio", status_code=404)
    conn_f, port_f, host_f, live_f, test_f = _RADIO_FIELDS[name]
    form = await request.form()
    db, tx = _db(request), _tx(request)
    conn = str(form.get(conn_f, "serial") or "serial").strip()
    port = str(form.get(port_f, "") or "").strip()
    host = str(form.get(host_f, "") or "").strip()
    def selected(field, default):
        try:
            return int(form.get(field, db.get_setting(field, default)))
        except (TypeError, ValueError):
            return default
    values = dict(conn=conn, port=port, host=host,
                  live_val=selected(live_f, 0), test_val=selected(test_f, 1))
    channels, model, error = await tx.load_channels(name, conn, port, host)
    if channels is not None:
        db.set_setting(name + "_channels", channels)
        db.set_setting(name + "_model", model)
        db.set_setting(name + "_channels_target", _channel_target(conn, port, host))
        ctx = _channel_ctx(db, tx, name, channels=channels, model=model,
                           error="", connected=True, **values)
    else:
        ctx = _channel_ctx(db, tx, name, channels=[], model="",
                           error=error or "could not read this radio", connected=False,
                           **values)
    return render(request, "_radio_channels.html", **ctx)


@router.post("/settings/detect-ports", response_class=HTMLResponse)
async def detect_meshcore_ports(request: Request):
    form = await request.form()
    current_port = str(form.get("meshcore_port", "") or "").strip()
    devices = list_usb_serial_devices()
    return render(
        request,
        "_meshcore_ports.html",
        devices=devices,
        current_port=current_port,
    )


@router.get("/meshcore/settings", response_class=HTMLResponse)
async def meshcore_settings_page(request: Request, saved: str = ""):
    device = None
    error = ""
    try:
        device = await _tx(request).get_device_settings()
    except RuntimeError as exc:
        error = str(exc)
    saved_labels = {"name": "Device name", "channel": "Channel name",
                    "added": "Channel", "removed": "Channel removal",
                    "power": "TX power", "radio": "Radio parameters"}
    return render(request, "meshcore_settings.html", device=device,
                  error=error, saved_label=saved_labels.get(saved, ""),
                  live_channel=int(_db(request).get_setting("meshcore_channel", 0)),
                  test_channel=int(_db(request).get_setting("meshcore_test_channel", 1)))


async def _device_edit_error(request: Request, error: str, status_code: int):
    try:
        device = await _tx(request).get_device_settings()
    except RuntimeError:
        device = None
    response = render(request, "meshcore_settings.html", device=device,
                      error=error, saved_label="",
                      live_channel=int(_db(request).get_setting("meshcore_channel", 0)),
                      test_channel=int(_db(request).get_setting("meshcore_test_channel", 1)))
    response.status_code = status_code
    return response


@router.post("/meshcore/settings/name", response_class=HTMLResponse)
async def save_meshcore_name(request: Request, name: str = Form(...)):
    try:
        await _tx(request).set_device_name(name)
    except ValueError as exc:
        return await _device_edit_error(request, str(exc), 400)
    except RuntimeError as exc:
        return await _device_edit_error(request, str(exc), 503)
    return RedirectResponse("/meshcore/settings?saved=name", status_code=303)


@router.post("/meshcore/settings/channel/{index}", response_class=HTMLResponse)
async def save_meshcore_channel(request: Request, index: int, name: str = Form(...)):
    try:
        await _tx(request).rename_device_channel(index, name)
    except ValueError as exc:
        return await _device_edit_error(request, str(exc), 400)
    except RuntimeError as exc:
        return await _device_edit_error(request, str(exc), 503)
    return RedirectResponse("/meshcore/settings?saved=channel", status_code=303)


@router.post("/meshcore/settings/channels/add", response_class=HTMLResponse)
async def add_meshcore_channel(request: Request, name: str = Form(...),
                               secret_hex: str = Form("")):
    try:
        created = await _tx(request).add_device_channel(name, secret_hex)
    except ValueError as exc:
        return await _device_edit_error(request, str(exc), 400)
    except RuntimeError as exc:
        return await _device_edit_error(request, str(exc), 503)
    try:
        device = await _tx(request).get_device_settings()
        error = created.get("refresh_error", "")
    except RuntimeError as exc:
        device = None
        error = f"Channel was added, but settings could not be refreshed: {exc}"
    response = render(request, "meshcore_settings.html", device=device, error=error,
                      saved_label="", created_channel=created,
                      live_channel=int(_db(request).get_setting("meshcore_channel", 0)),
                      test_channel=int(_db(request).get_setting("meshcore_test_channel", 1)))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/meshcore/settings/channel/{index}/remove", response_class=HTMLResponse)
async def remove_meshcore_channel(request: Request, index: int):
    try:
        await _tx(request).remove_device_channel(index)
    except ValueError as exc:
        return await _device_edit_error(request, str(exc), 400)
    except RuntimeError as exc:
        return await _device_edit_error(request, str(exc), 503)
    return RedirectResponse("/meshcore/settings?saved=removed", status_code=303)


@router.post("/meshcore/settings/tx-power", response_class=HTMLResponse)
async def save_meshcore_tx_power(request: Request, power: int = Form(...)):
    try:
        await _tx(request).set_tx_power(power)
    except ValueError as exc:
        return await _device_edit_error(request, str(exc), 400)
    except RuntimeError as exc:
        return await _device_edit_error(request, str(exc), 503)
    return RedirectResponse("/meshcore/settings?saved=power", status_code=303)


@router.post("/meshcore/settings/radio", response_class=HTMLResponse)
async def save_meshcore_radio(request: Request, freq: float = Form(...), bw: float = Form(...),
                              sf: int = Form(...), cr: int = Form(...)):
    try:
        await _tx(request).set_radio_parameters(freq, bw, sf, cr)
    except ValueError as exc:
        return await _device_edit_error(request, str(exc), 400)
    except RuntimeError as exc:
        return await _device_edit_error(request, str(exc), 503)
    return RedirectResponse("/meshcore/settings?saved=radio", status_code=303)


# ---- manual send -------------------------------------------------------
@router.get("/manual", response_class=HTMLResponse)
async def manual_page(request: Request):
    return render(request, "manual_send.html")


@router.post("/manual/send", response_class=HTMLResponse)
async def manual_send(request: Request, text: str = Form(...)):
    tx = _tx(request)
    budget = getattr(tx, "message_budget", MAX_PAYLOAD_BYTES)
    if len(text.encode("utf-8")) > budget:
        return render(request, "_manual_result.html", ok=False,
                      message=f"message exceeds MeshCore limit ({budget} bytes)",
                      text=text, bytes=len(text.encode("utf-8")))
    ok = await tx.send_manual(text)   # goes on each radio's LIVE channel
    msg = "sent" if ok else f"failed: {tx.last_error}"
    return render(request, "_manual_result.html", ok=ok, message=msg,
                  text=text, bytes=len(text.encode()))


# ---- troubleshoot ------------------------------------------------------
@router.get("/troubleshoot", response_class=HTMLResponse)
async def troubleshoot(request: Request):
    db = _db(request)
    db_path = db.path
    db_size = 0
    if db_path != ":memory:":
        try:
            db_size = Path(db_path).stat().st_size
        except OSError:
            pass
    return render(request, "troubleshoot.html",
                  errors=db.recent_errors(), transports=_tx(request).status(),
                  db_path=db_path, db_size=db_size)


@router.post("/troubleshoot/clear-errors", response_class=HTMLResponse)
async def clear_errors(request: Request):
    _db(request).clear_errors()
    return render(request, "_errors.html", errors=[])


@router.post("/troubleshoot/test", response_class=HTMLResponse)
async def send_test(request: Request):
    tx = _tx(request)
    text = "WXEcho test message"
    ok = await tx.send_test(text)   # goes on each radio's TEST channel
    return render(
        request, "_manual_result.html", ok=ok,
        message=("test sent to all radios" if ok else f"failed: {tx.last_error}"),
        text=text, bytes=len(text.encode()),
    )


@router.post("/troubleshoot/test/{name}", response_class=HTMLResponse)
async def send_test_one(request: Request, name: str):
    tx = _tx(request)
    label = {t["name"]: t["label"] for t in tx.status()}.get(name, name)
    text = "WXEcho test via %s" % label
    ok, err = await tx.send_to(name, text)
    return render(
        request, "_manual_result.html", ok=ok,
        message=(f"test sent via {label}" if ok else f"{label} failed: {err}"),
        text=text, bytes=len(text.encode()),
    )


@router.get("/troubleshoot/raw", response_class=PlainTextResponse)
async def raw_response(request: Request):
    raw = _poller(request).status.last_raw_response
    return raw or "(no BOM response captured yet)"


def _split_lines(value: str) -> list[str]:
    """Parse a textarea/newline- or comma-separated list into clean items."""
    parts: list[str] = []
    for chunk in value.replace(",", "\n").splitlines():
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts

