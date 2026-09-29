from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    assert context["recent"][0]["transmit_status"] == "success"


def test_dashboard_coverage_uses_states_and_forecast_districts():
    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(db=DashboardDB(), tx=DashboardTx(), poller=DashboardPoller())
    ))

    context = _dash_ctx(request)

    assert context["state_count"] == 1
    assert context["district_count"] == 0
    assert context["all_districts"] is True


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
    assert "Watching 1 state/territory feed" in dashboard.text
    assert "all forecast districts" in dashboard.text
    assert "/dev/serial/by-id/usb-Seeed_XIAO-if00" in dashboard.text
    assert "delivered" in dashboard.text
    assert history.status_code == 200
    assert "Severe Weather Warning" in history.text
    assert "delivered" in history.text
