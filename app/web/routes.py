"""Web UI routes (server-rendered templates + htmx partials)."""
from __future__ import annotations

import re
import sys
from pathlib import Path
import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import MAX_PAYLOAD_BYTES, POLL_INTERVAL_MIN
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
}

TX_STATUS_LABELS = {
    "queued": "queued",
    "success": "delivered",
    "failed": "failed",
    "dry-run": "dry-run",
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
    regions = list(db.get_setting("bom_regions", ["NSW"]) or [])
    zones = list(db.get_setting("bom_districts", []) or [])
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
    rows = db.query_history(limit=1000)
    sent = [r for r in rows if r["transmit_status"] == "success"]
    sent_7d = sum(1 for r in sent if _age(r["ts"]) < 7)
    sent_today = sum(1 for r in sent if _age(r["ts"]) < 1)
    buckets = [0] * 7
    for r in sent:
        a = _age(r["ts"])
        if 0 <= a < 7:
            buckets[6 - int(a)] += 1
    spark_line, spark_fill = _spark(buckets)
    include = list(db.get_setting("filter_include_exact", []) or [])
    if db.get_setting("filter_include_suffix", []):
        include = ["All Warnings"] + include
    recent_rows = []
    seen_alert_ids = set()
    for r in db.query_history(limit=200):
        alert_id = r["alert_id"] if "alert_id" in r.keys() else None
        if alert_id and alert_id in seen_alert_ids:
            continue
        if alert_id:
            seen_alert_ids.add(alert_id)
        recent_rows.append(r)
        if len(recent_rows) == 6:
            break
    recent = [{
        "event": r["event"], "area": r["area"], "disposition": r["disposition"],
        "transmit_status": r["transmit_status"], "detail": r["detail"],
        "text": r["transmitted_text"], "when": fmt_local(r["ts"], tz),
    } for r in recent_rows]
    ltx = db.query_transmit_log(limit=1)
    last_tx = "-"
    if ltx:
        # ASCII only: this string is auto-escaped through {{ }}, so a non-ASCII
        # separator could mojibake depending on charset. Keep it plain.
        last_tx = ("OK - " if ltx[0]["success"] else "failed - ") + fmt_local(ltx[0]["ts"], tz)

    # ---- health watchdog: catch SILENT failures (looks live, delivers nothing) --
    st = poller.status
    interval = int(db.get_setting("poll_interval", 120))
    dry = bool(db.get_setting("dry_run", True))
    def _age_s(ts):
        try:
            return (now - datetime.datetime.fromisoformat(ts)).total_seconds()
        except Exception:
            return None
    problems = []          # (level, message); level in {"critical","warn"}
    # 1. Are we still reaching BOM? A stale success time = we are blind to alerts.
    succ_age = _age_s(st.last_poll_success_time)
    stale_after = max(interval * 3, 360)
    if succ_age is None:
        problems.append(("warn", "No successful BOM poll yet."))
    elif succ_age > stale_after:
        problems.append(("critical",
            "Not reaching BOM: last good poll %d min ago. Not receiving alerts." % (succ_age // 60)))
    if st.last_poll_result.startswith("error"):
        problems.append(("warn", "Last BOM poll errored: %s" % st.last_poll_result[7:][:80]))
    elif st.last_poll_result.startswith("partial"):
        problems.append(("warn", "Some BOM feeds failed: %s" % st.last_poll_result[9:][:80]))
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
        "state_count": len(regions), "district_count": len(zones),
        "all_districts": not zones,
        "poll_interval": int(db.get_setting("poll_interval", 120)), "queue_depth": tx.queue_depth,
        "last_poll_local": fmt_local(poller.status.last_poll_time, tz) if poller.status.last_poll_time else "-",
        "uptime_str": uptime_str, "sent_7d": sent_7d, "sent_today": sent_today,
        "spark_line": spark_line, "spark_fill": spark_fill,
        "include": include, "recent": recent, "last_tx": last_tx,
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
    # The recent-alerts list + radios + broadcasting columns, for live polling.
    return render(request, "_dash_cols.html", **_dash_ctx(request))


@router.post("/dry-run/toggle", response_class=HTMLResponse)
async def toggle_dry_run(request: Request):
    db = _db(request)
    db.set_setting("dry_run", not bool(db.get_setting("dry_run", True)))
    db.add_event("INFO", f"dry-run set to {db.get_setting('dry_run')}")
    return render(request, "_dash_top.html", **_dash_ctx(request))


# ---- history -----------------------------------------------------------
def _superseded_history_ids(rows):
    """Flag older versions of the same BOM item in the visible History page."""
    seen = set()
    superseded = set()
    for row in rows:
        if "alert_id" not in row.keys() or "id" not in row.keys():
            continue
        alert_id = row["alert_id"]
        if alert_id in seen:
            superseded.add(row["id"])
        else:
            seen.add(alert_id)
    return superseded


@router.get("/history", response_class=HTMLResponse)
async def history(request: Request, disposition: str = "", transmit_status: str = "",
                  date_from: str = "", date_to: str = ""):
    rows = _db(request).query_history(
        disposition=disposition or None,
        transmit_status=transmit_status or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    return render(
        request, "history.html", rows=rows, superseded_ids=_superseded_history_ids(rows), disposition=disposition,
        transmit_status=transmit_status, date_from=date_from, date_to=date_to,
        dispositions=["sent", "filtered", "update", "cancelled"],
        transmit_statuses=["queued", "success", "failed", "dry-run"],
    )


@router.get("/partials/history", response_class=HTMLResponse)
async def history_partial(request: Request, disposition: str = "", transmit_status: str = "",
                          date_from: str = "", date_to: str = ""):
    # Just the rows, honoring the same filters, for live polling.
    rows = _db(request).query_history(
        disposition=disposition or None,
        transmit_status=transmit_status or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    return render(request, "_history_rows.html", rows=rows,
                  superseded_ids=_superseded_history_ids(rows))


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
async def settings_page(request: Request):
    db = _db(request)
    s = db.all_settings()
    mc_connected = _status_flag(request, "meshcore")
    return render(
        request, "settings.html", s=s, min_interval=POLL_INTERVAL_MIN, ports=list_usb_serial_devices(),
        timezones=_TIMEZONES, tz_current=(s.get("display_timezone", "") or ""),
        tz_known={v for v, _ in _TIMEZONES},
        event_groups=_EVENT_GROUPS,
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


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    poll_interval: int = Form(...),
    display_timezone: str = Form("Australia/Sydney"),
    bom_regions: list[str] = Form(default=[]),
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
    interval = max(POLL_INTERVAL_MIN, int(poll_interval))

    db.set_setting("bom_regions", [r.strip().upper() for r in bom_regions if r.strip()])
    db.set_setting("bom_districts", [d.strip() for d in bom_districts.replace(",", "\n").splitlines() if d.strip()])
    db.set_setting("poll_interval", interval)
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

