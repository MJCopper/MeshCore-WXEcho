from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web.routes import router


class FakeDB:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})

    def all_settings(self):
        return dict(self._settings)

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def set_setting(self, key, value):
        self._settings[key] = value


class FakeTx:
    def __init__(self, load_result, connected=False):
        self._load_result = load_result
        self._connected = connected
        self.calls = []

    async def load_channels(self, name, conn, port, host):
        self.calls.append((name, conn, port, host))
        return self._load_result

    def status(self):
        return [{"name": "meshcore", "connected": self._connected}]


def _client(db_settings, load_result=(([], "", "")), connected=False):
    app = FastAPI()
    app.include_router(router)
    app.state.db = FakeDB(db_settings)
    app.state.tx = FakeTx(load_result=load_result, connected=connected)
    app.state.poller = object()
    return TestClient(app), app.state.db, app.state.tx


def test_settings_page_renders_load_button_and_numeric_fallback():
    client, _, _ = _client(
        {
            "meshcore_enabled": True,
            "meshcore_conn": "serial",
            "meshcore_port": "/dev/ttyUSB0",
            "meshcore_host": "",
            "meshcore_channel": 2,
            "meshcore_test_channel": 5,
            "meshcore_channels": [],
            "meshcore_model": "",
            "display_timezone": "Australia/Sydney",
        }
    )

    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    assert "hx-post=\"/settings/channels/meshcore\"" in body
    assert "hx-target=\"#meshcore-channel-fields\"" in body
    assert "hx-include=\"[name='meshcore_conn'],[name='meshcore_port'],[name='meshcore_host']\"" in body
    assert "<div id=\"meshcore-channel-fields\">" in body
    assert 'name="meshcore_channel" type="number" min="0" value="2"' in body
    assert 'name="meshcore_test_channel" type="number" min="0" value="5"' in body


def test_settings_page_renders_selects_when_channels_exist():
    client, _, _ = _client(
        {
            "meshcore_enabled": True,
            "meshcore_conn": "serial",
            "meshcore_port": "/dev/ttyUSB0",
            "meshcore_host": "",
            "meshcore_channel": 3,
            "meshcore_test_channel": 7,
            "meshcore_channels": [
                {"index": 0, "name": "Public"},
                {"index": 3, "name": "Ops"},
            ],
            "meshcore_model": "Heltec V3",
            "display_timezone": "Australia/Sydney",
        },
        connected=True,
    )

    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    assert 'name="meshcore_channel"' in body
    assert 'name="meshcore_test_channel"' in body
    assert "<select name=\"meshcore_channel\">" in body
    assert "3 - Ops" in body
    assert "selected>3 - Ops" in body
    # test_channel=7 is preserved via synthetic option because it is missing from radio list
    assert "selected>7 - current" in body


def test_load_channels_success_renders_named_selects_and_caches_channels():
    client, db, tx = _client(
        {
            "meshcore_conn": "serial",
            "meshcore_port": "/dev/ttyUSB0",
            "meshcore_host": "",
            "meshcore_channel": 1,
            "meshcore_test_channel": 2,
            "meshcore_channels": [],
            "meshcore_model": "",
        },
        load_result=(
            [{"index": 1, "name": "Live"}, {"index": 2, "name": "Bench"}],
            "Heltec V3",
            "",
        ),
    )

    resp = client.post(
        "/settings/channels/meshcore",
        data={"meshcore_conn": "serial", "meshcore_port": "/dev/ttyUSB0", "meshcore_host": ""},
    )

    assert resp.status_code == 200
    body = resp.text
    assert "<select name=\"meshcore_channel\">" in body
    assert "1 - Live" in body
    assert "2 - Bench" in body
    assert "Detected radio: Heltec V3" in body
    assert db.get_setting("meshcore_channels") == [
        {"index": 1, "name": "Live"},
        {"index": 2, "name": "Bench"},
    ]
    assert db.get_setting("meshcore_model") == "Heltec V3"
    assert tx.calls == [("meshcore", "serial", "/dev/ttyUSB0", "")]


def test_load_channels_error_shows_error_and_clears_stale_options():
    client, db, _ = _client(
        {
            "meshcore_conn": "serial",
            "meshcore_port": "/dev/ttyUSB0",
            "meshcore_host": "",
            "meshcore_channel": 4,
            "meshcore_test_channel": 6,
            "meshcore_channels": [{"index": 9, "name": "StaleChannel"}],
            "meshcore_model": "Old Model",
        },
        load_result=(None, "", "radio timeout"),
    )

    resp = client.post(
        "/settings/channels/meshcore",
        data={"meshcore_conn": "serial", "meshcore_port": "/dev/ttyUSB0", "meshcore_host": ""},
    )

    assert resp.status_code == 200
    body = resp.text
    assert "radio timeout" in body
    assert "StaleChannel" not in body
    assert "<select name=\"meshcore_channel\">" not in body
    assert 'name="meshcore_channel" type="number" min="0" value="4"' in body
    assert 'name="meshcore_test_channel" type="number" min="0" value="6"' in body
    # The DB cache remains untouched on load failure.
    assert db.get_setting("meshcore_channels") == [{"index": 9, "name": "StaleChannel"}]
    assert db.get_setting("meshcore_model") == "Old Model"
