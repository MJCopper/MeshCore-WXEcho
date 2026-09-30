from fastapi import FastAPI
from fastapi.testclient import TestClient

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


def test_settings_page_renders_persistent_dropdowns_and_auto_refresh():
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
    assert 'id="meshcore_detected_port"' in body
    assert 'hx-post="/settings/detect-ports"' in body
    assert 'name="meshcore_channel"' in body
    assert 'name="meshcore_test_channel"' in body
    assert 'selected>2 - current (names unavailable)' in body
    assert 'selected>5 - current (names unavailable)' in body
    assert "fetch('/settings/channels/meshcore'" in body
    assert 'Load channels</button>' not in body
    assert 'name="meshcore_enabled"' not in body
    assert "Contact / User-Agent" not in body



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


def test_detect_ports_renders_select_and_saved_path(monkeypatch):
    path = "/dev/serial/by-id/usb-Seeed_Studio_XIAO_nRF52840_B89FC3F98AFD92B1-if00"
    other = "/dev/serial/by-id/usb-Other_Device-if00"
    client, _, _ = _client({"meshcore_conn": "serial", "meshcore_port": path})
    monkeypatch.setattr(
        "app.web.routes.list_usb_serial_devices", lambda: [other, path]
    )

    response = client.post("/settings/detect-ports", data={"meshcore_port": path})

    assert response.status_code == 200
    assert '<select id="meshcore_detected_port"' in response.text
    assert f'value="{path}" selected' in response.text
    assert f'value="{other}"' in response.text
    assert "USB serial device" in response.text
    assert "port.dispatchEvent(new Event('input'" in response.text


def test_detect_ports_renders_empty_state(monkeypatch):
    client, _, _ = _client({"meshcore_conn": "serial", "meshcore_port": ""})
    monkeypatch.setattr("app.web.routes.list_usb_serial_devices", lambda: [])

    response = client.post("/settings/detect-ports", data={"meshcore_port": ""})

    assert response.status_code == 200
    assert "No devices found in /dev/serial/by-id/." in response.text
    assert 'id="meshcore_detected_port"' in response.text


def test_detect_ports_keeps_saved_path_when_device_is_unplugged(monkeypatch):
    path = "/dev/serial/by-id/usb-Seeed_Studio_XIAO_nRF52840_B89FC3F98AFD92B1-if00"
    client, _, _ = _client({"meshcore_port": path})
    monkeypatch.setattr("app.web.routes.list_usb_serial_devices", lambda: [])

    response = client.post("/settings/detect-ports", data={"meshcore_port": path})

    assert f'value="{path}" selected' in response.text
    assert "saved path, currently unavailable" in response.text


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
            "meshcore_channels_target": {"conn": "serial", "target": "/dev/ttyUSB0"},
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
    assert db.get_setting("meshcore_channels_target") == {"conn": "serial", "target": "/dev/ttyUSB0"}
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
    assert "<select name=\"meshcore_channel\">" in body
    assert "selected>4 - current (names unavailable)" in body
    assert "selected>6 - current (names unavailable)" in body
    # The DB cache remains untouched on load failure.
    assert db.get_setting("meshcore_channels") == [{"index": 9, "name": "StaleChannel"}]
    assert db.get_setting("meshcore_model") == "Old Model"


def test_channel_refresh_keeps_unsaved_channel_selections():
    client, db, _ = _client(
        {"meshcore_conn": "serial", "meshcore_port": "/dev/ttyACM0",
         "meshcore_channel": 1, "meshcore_test_channel": 2},
        load_result=([{"index": 3, "name": "Weather"}], "Companion", ""),
    )

    response = client.post("/settings/channels/meshcore", data={
        "meshcore_conn": "serial", "meshcore_port": "/dev/ttyACM0",
        "meshcore_channel": "3", "meshcore_test_channel": "5",
    })

    assert "selected>3 - Weather" in response.text
    assert "selected>5 - current" in response.text
    assert db.get_setting("meshcore_channel") == 1
    assert db.get_setting("meshcore_test_channel") == 2


def test_cached_channel_names_are_tied_to_connection_target():
    client, _, _ = _client({
        "meshcore_conn": "serial", "meshcore_port": "/dev/ttyACM1",
        "meshcore_channel": 3, "meshcore_test_channel": 4,
        "meshcore_channels": [{"index": 3, "name": "Old radio"}],
        "meshcore_channels_target": {"conn": "serial", "target": "/dev/ttyACM0"},
    })

    response = client.get("/settings")

    assert "Old radio" not in response.text
    assert "selected>3 - current (names unavailable)" in response.text


def test_saving_settings_forces_meshcore_enabled():
    client, db, _ = _client({"meshcore_enabled": False})

    response = client.post("/settings", data={
        "poll_interval": "120",
        "meshcore_conn": "serial", "meshcore_port": "/dev/ttyACM0",
        "meshcore_channel": "3", "meshcore_test_channel": "5",
    }, follow_redirects=False)

    assert response.status_code == 303
    assert db.get_setting("meshcore_enabled") is True
    assert db.get_setting("meshcore_channel") == 3
    assert db.get_setting("meshcore_test_channel") == 5


def test_all_warnings_disables_only_warning_product_choices():
    import re

    client, _, _ = _client({
        "filter_include_exact": ["Marine Wind Warning", "Flood Watch"],
        "filter_include_suffix": ["Warning"],
    })
    body = client.get("/settings").text
    assert 'id="warning-products" class="warning-products is-disabled" aria-disabled="true"' in body
    assert re.search(r'value="Marine Wind Warning" checked disabled', body)
    assert re.search(r'value="Flood Watch" checked>', body)
    assert "Included automatically by All BOM warning products" in body
    assert "allWarnings.addEventListener('change', updateWarningChoices)" in body


def test_warning_product_choices_survive_all_warnings_save():
    client, db, _ = _client({
        "filter_include_exact": ["Marine Wind Warning", "Flood Watch"],
        "filter_include_suffix": ["Warning"],
    })
    base = {"poll_interval": "120", "display_timezone": "Australia/Sydney"}

    response = client.post("/settings", data={
        **base, "all_warnings": "on", "events": ["Road Weather Alert"]},
        follow_redirects=False)
    assert response.status_code == 303
    assert db.get_setting("filter_include_exact") == ["Marine Wind Warning", "Road Weather Alert"]

    response = client.post("/settings", data={
        **base, "events": ["Marine Wind Warning", "Road Weather Alert"]},
        follow_redirects=False)
    assert response.status_code == 303
    assert db.get_setting("filter_include_exact") == ["Marine Wind Warning", "Road Weather Alert"]
    assert db.get_setting("filter_include_suffix") == []


def test_javascript_submission_preserves_new_warning_choices_while_all_selected():
    client, db, _ = _client({"filter_include_exact": ["Marine Wind Warning"]})
    response = client.post("/settings", data={
        "poll_interval": "120", "display_timezone": "Australia/Sydney",
        "all_warnings": "on", "warning_choices_submitted": "1",
        "events": ["Flood Warning", "Flood Watch"],
    }, follow_redirects=False)
    assert response.status_code == 303
    assert db.get_setting("filter_include_exact") == ["Flood Warning", "Flood Watch"]
