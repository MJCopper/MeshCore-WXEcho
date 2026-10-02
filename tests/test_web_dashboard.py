from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Database
from app.transmit import TransmitManager
from app.web.routes import _dash_ctx, router


class DashboardDB:
    def __init__(self):
        self.settings = {
            "bom_regions": ["NSW"],
            "bom_districts": [],
            "display_timezone": "Australia/Sydney",
            "poll_interval": 120,
            "dry_run": False,
            "filter_include_exact": ["Flood Watch"],
            "filter_include_suffix": ["Warning"],
            "meshcore_enabled": True,
            "meshcore_conn": "serial",
            "meshcore_port": "/dev/serial/by-id/usb-Seeed_XIAO-if00",
            "meshcore_channel": 2,
        }
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.history = [
            self._history_row(now, "success"),
            self._history_row(now, "failed"),
            self._history_row(now, "dry-run"),
        ]

    @staticmethod
    def _history_row(timestamp, transmit_status):
        return {
            "ts": timestamp,
            "event": "Severe Weather Warning",
            "area": "NSW",
            "disposition": "sent",
            "transmit_status": transmit_status,
            "detail": "new alert",
            "transmitted_text": "warning text",
        }

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def query_history(self, limit=200, **kwargs):
        return self.history[:limit]

    def query_service_history(self, limit=200, **kwargs):
        return [dict(row, id=i, source="bom", external_id=str(i), title=row["event"],
                     metadata={}) for i, row in enumerate(self.history[:limit], 1)]

    def query_transmit_log(self, limit=200):
        return []


class DashboardTx:
    port = "/dev/serial/by-id/usb-Seeed_XIAO-if00"
    connected = True
    last_error = ""
    queue_depth = 0

    def status(self):
        return [{
            "name": "meshcore",
            "label": "MeshCore",
            "enabled": True,
            "conn": "serial",
            "connected": True,
            "target": self.port,
            "channel": 2,
            "error": "",
        }]


class DashboardPoller:
    status = SimpleNamespace(
        uptime_seconds=300,
        last_poll_time=None,
        last_poll_success_time=None,
        last_poll_result="not yet polled",
        last_broadcast_failure=None,
        last_broadcast_failure_text="",
        clock_skew_seconds=None,
    )


def test_dashboard_counts_only_confirmed_meshcore_broadcasts():
    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(db=DashboardDB(), tx=DashboardTx(), poller=DashboardPoller())
    ))

    context = _dash_ctx(request)

    assert context["sent_7d"] == 1
    assert context["sent_today"] == 1
    assert [row["transmit_status"] for row in context["recent"]] == ["success"]


def test_recent_notices_only_show_successful_sends_across_services():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for index, source in enumerate(["bom", "rfs", "rfs", "rfs", "rfs", "traffic", "traffic"], 1):
        rows.append({"id": index, "ts": now, "source": source,
                     "external_id": f"{source}-{index}", "title": f"Notice {index}",
                     "area": "NSW", "disposition": "sent", "transmit_status": "success",
                     "detail": "", "transmitted_text": f"text {index}"})
    rows.append(dict(rows[-1], id=8, title="Dry Run", transmit_status="dry-run"))
    rows.append(dict(rows[-1], id=9, title="Failed", transmit_status="failed"))
    db = DashboardDB()
    db.query_service_history = lambda **kwargs: rows[:kwargs.get("limit", len(rows))]
    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(db=db, tx=DashboardTx(), poller=DashboardPoller())
    ))
    recent = _dash_ctx(request)["recent"]
    assert len(recent) == 6
    assert all(row["transmit_status"] == "success" for row in recent)
    assert sum(row["source"] == "NSW RFS" for row in recent) == 4
    assert recent[0]["event"] == "Notice 7"


def test_dashboard_describes_all_three_services():
    db = DashboardDB()
    db.settings.update({"rfs_enabled": True, "rfs_poll_minutes": 15,
                        "traffic_enabled": True, "traffic_poll_minutes": 20})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        db=db, tx=DashboardTx(), poller=DashboardPoller(),
        rfs_poller=SimpleNamespace(last_poll=now, last_successful_poll=now,
                                   last_result="ok: 2 RFS incidents"),
        traffic_poller=SimpleNamespace(last_poll=now, last_successful_poll=now,
                                       last_result="ok: 10 traffic items"),
    )))
    context = _dash_ctx(request)
    assert [service["label"] for service in context["enabled_services"]] == [
        "BOM", "NSW RFS", "Live Traffic NSW"]
    assert [service["interval"] for service in context["services"]] == [5, 15, 20]
    assert context["services"][1]["state"] == "ok"
    assert context["services"][2]["state"] == "ok"


def test_meshcore_status_exposes_configured_target():
    manager = TransmitManager(DashboardDB())

    assert manager.status()[0]["target"] == "/dev/serial/by-id/usb-Seeed_XIAO-if00"


def test_dashboard_and_history_render_populated_data():
    app = FastAPI()
    app.include_router(router)
    app.state.db = DashboardDB()
    app.state.tx = DashboardTx()
    app.state.poller = DashboardPoller()
    client = TestClient(app)

    dashboard = client.get("/")
    history = client.get("/history")

    assert dashboard.status_code == 200
    assert "Recent Notices" in dashboard.text
    assert "Locally transmitted by this radio" in dashboard.text
    assert "NSW RFS" in dashboard.text
    assert "Live Traffic NSW" in dashboard.text
    assert "/dev/serial/by-id/usb-Seeed_XIAO-if00" in dashboard.text
    assert "transmitted" in dashboard.text
    assert history.status_code == 200
    assert "Severe Weather Warning" in history.text
    assert "transmitted" in history.text


def test_disabled_bom_does_not_raise_missing_feed_health_warning():
    db = DashboardDB()
    db.settings["bom_enabled"] = False
    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(db=db, tx=DashboardTx(), poller=DashboardPoller())
    ))
    context = _dash_ctx(request)
    assert context["bom_enabled"] is False
    assert context["services"][0]["state"] == "disabled"
    assert not any("BOM poll" in problem for problem in context["health_problems"])


def test_successful_activity_counts_all_service_sources():
    db = Database(":memory:")
    now = datetime.now(timezone.utc)
    for source in ("bom", "rfs", "traffic"):
        db.add_service_history(source, source, source, transmit_status="success")
    db.add_service_history("traffic", "dry", "Dry Run", transmit_status="dry-run")
    db.add_service_history("bom", "failed", "Failed", transmit_status="failed")
    activity = db.successful_service_activity(now)
    assert activity == {0: 3}
    db.close()
