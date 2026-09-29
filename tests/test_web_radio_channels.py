from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.meshcore_discovery import DetectedMeshCore
from app.web.routes import router


class FakeDB:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})
        self.events = []

    def all_settings(self):
        return dict(self._settings)

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def set_setting(self, key, value):
        self._settings[key] = value

    def add_event(self, level, message):
        self.events.append((level, message))


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

    async def reconfigure(self):
        self.calls.append(("reconfigure",))


class FakePoller:
    def __init__(self):
        self.poked = False

    def poke(self):
        self.poked = True


def _client(db_settings, load_result=(([], "", "")), connected=False):
    app = FastAPI()
    app.include_router(router)
    app.state.db = FakeDB(db_settings)
    app.state.tx = FakeTx(load_result=load_result, connected=connected)
    app.state.poller = FakePoller()
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
    assert 'hx-include="#meshcore_port"' in body
    assert 'name="meshcore_port"' in body
    assert "USB serial port (manual path)" in body
    assert "<div id=\"meshcore-channel-fields\">" in body
    assert 'name="meshcore_channel" type="number" min="0" value="2"' in body
    assert 'name="meshcore_test_channel" type="number" min="0" value="5"' in body


def test_settings_page_uses_australian_bom_products():
    client, _, _ = _client(
        {
            "filter_include_exact": [],
            "filter_include_suffix": ["Warning"],
            "display_timezone": "Australia/Sydney",
        }
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Severe Weather Warning" in response.text
    assert "Marine Wind Warning" in response.text
    assert "Warning to Sheep Graziers" in response.text
    assert "Flood Watch" in response.text
    assert "Tropical Cyclone Advice" in response.text
    assert "Road Weather Alert" in response.text
    assert "Hurricane Warning" not in response.text
    assert "Winter Weather Advisory" not in response.text


def test_save_settings_keeps_only_known_bom_products():
    client, db, tx = _client({})

    response = client.post(
        "/settings",
        data={
            "poll_interval": "120",
            "bom_contact": "operator@example.com",
            "display_timezone": "Australia/Sydney",
            "events": ["Flood Watch", "Road Weather Alert", "Hurricane Warning"],
            "all_warnings": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert db.get_setting("filter_include_exact") == ["Flood Watch", "Road Weather Alert"]
    assert db.get_setting("filter_include_suffix") == ["Warning"]
    assert ("reconfigure",) in tx.calls


def test_detect_ports_renders_select_and_active_radio(monkeypatch):
    path = "/dev/serial/by-id/usb-Seeed_Studio_XIAO_nRF52840_B89FC3F98AFD92B1-if00"
    client, _, _ = _client(
        {
            "meshcore_conn": "serial",
            "meshcore_port": path,
            "meshcore_model": "Companion",
        },
        connected=True,
    )
    calls = []

    async def fake_discovery(**kwargs):
        calls.append(kwargs)
        return [DetectedMeshCore(
            port=path,
            description="XIAO nRF52840",
            model="Companion",
            firmware="1.9.0",
        )]

    monkeypatch.setattr("app.web.routes.find_meshcore_devices", fake_discovery)
    response = client.post("/settings/detect-ports", data={"meshcore_port": path})

    assert response.status_code == 200
    assert '<select id="meshcore_detected_port"' in response.text
    assert f'value="{path}" selected' in response.text
    assert "Companion (firmware 1.9.0)" in response.text
    assert "XIAO nRF52840" in response.text
    assert "document.getElementById('meshcore_port').value" in response.text
    assert calls == [{"active_port": path, "active_model": "Companion"}]


def test_detect_ports_renders_empty_state(monkeypatch):
    client, _, _ = _client({"meshcore_conn": "serial", "meshcore_port": ""})

    async def no_devices(**kwargs):
        return []

    monkeypatch.setattr("app.web.routes.find_meshcore_devices", no_devices)
    response = client.post("/settings/detect-ports", data={"meshcore_port": ""})

    assert response.status_code == 200
    assert "No MeshCore devices responded" in response.text
    assert "meshcore_detected_port" not in response.text


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
