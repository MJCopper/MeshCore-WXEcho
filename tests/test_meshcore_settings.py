import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from meshcore import EventType

from app.transmit import MeshCoreTransmitter, TransmitManager
from app.web.routes import router


@pytest.mark.asyncio
async def test_device_snapshot_omits_keys_and_pin():
    class Commands:
        async def send_device_query(self):
            return SimpleNamespace(type=EventType.DEVICE_INFO, payload={
                "model": "Companion", "ver": "1.4", "max_channels": 1, "ble_pin": 123456,
            })

        async def send_appstart(self):
            return SimpleNamespace(type=EventType.SELF_INFO, payload={
                "name": "Weather node", "public_key": "secret", "radio_freq": 915.0,
            })

        async def get_bat(self):
            return SimpleNamespace(type=EventType.BATTERY, payload={"level": 3900})

        async def get_channel(self, index):
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_name": "Alerts", "channel_secret": b"private-key-bytes",
                "channel_hash": "ab",
            })

    transmitter = MeshCoreTransmitter("serial", "/dev/ttyUSB0")
    transmitter._mc = SimpleNamespace(is_connected=True, commands=Commands())
    result = await transmitter.read_settings()

    assert result["name"] == "Weather node"
    assert result["battery_mv"] == 3900
    assert result["channels"] == [{"index": 0, "name": "Alerts", "hash": "ab"}]
    assert "ble_pin" not in str(result)
    assert "secret" not in str(result)

    async def battery_unavailable():
        raise RuntimeError("unsupported")

    transmitter._mc.commands.get_bat = battery_unavailable
    result = await transmitter.read_settings()
    assert result["battery_mv"] is None
    assert result["channels"][0]["name"] == "Alerts"


@pytest.mark.asyncio
async def test_name_write_requires_successful_readback():
    class Commands:
        async def set_name(self, name):
            assert name == "New node"
            return SimpleNamespace(type=EventType.OK)

        async def send_appstart(self):
            return SimpleNamespace(type=EventType.SELF_INFO, payload={"name": "New node"})

    transmitter = MeshCoreTransmitter("serial")
    transmitter._mc = SimpleNamespace(is_connected=True, commands=Commands())
    assert await transmitter.set_device_name("New node") == "New node"

    async def stale_name():
        return SimpleNamespace(type=EventType.SELF_INFO, payload={"name": "Old node"})

    transmitter._mc.commands.send_appstart = stale_name
    with pytest.raises(RuntimeError, match="verify"):
        await transmitter.set_device_name("New node")


@pytest.mark.asyncio
async def test_channel_rename_preserves_secret_and_rejects_hash_names():
    secret = bytes(range(16))

    class Commands:
        def __init__(self):
            self.calls = []
            self.name = "Alerts"

        async def get_channel(self, index):
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_name": self.name, "channel_secret": secret, "channel_hash": "ab",
            })

        async def set_channel(self, index, name, saved_secret):
            self.calls.append((index, name, saved_secret))
            self.name = name
            return SimpleNamespace(type=EventType.OK)

    transmitter = MeshCoreTransmitter("serial")
    commands = Commands()
    transmitter._mc = SimpleNamespace(is_connected=True, commands=commands)
    assert await transmitter.rename_channel(0, "Weather") == {
        "index": 0, "name": "Weather", "hash": "ab",
    }
    assert commands.calls == [(0, "Weather", secret)]
    with pytest.raises(ValueError, match="cannot start with #"):
        await transmitter.rename_channel(0, "#Public")
    assert len(commands.calls) == 1


@pytest.mark.asyncio
async def test_channel_rename_rejects_changed_secret_on_readback():
    secret = b"0" * 16

    class Commands:
        async def get_channel(self, index):
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_name": "Ops", "channel_secret": secret if not self.changed else b"1" * 16,
            })

        async def set_channel(self, index, name, saved_secret):
            self.changed = True
            return SimpleNamespace(type=EventType.OK)

        changed = False

    transmitter = MeshCoreTransmitter("serial")
    transmitter._mc = SimpleNamespace(is_connected=True, commands=Commands())
    with pytest.raises(RuntimeError, match="key preservation"):
        await transmitter.rename_channel(1, "Ops")


def _web_client(connected=True):
    class Database:
        def get_setting(self, key, default=None):
            return default

    class Radio:
        def __init__(self):
            self.calls = []

        async def get_device_settings(self):
            if not connected:
                raise RuntimeError("saved MeshCore radio is offline")
            return {"name": "Weather node", "model": "Companion", "firmware": "1.4",
                    "battery_mv": 3900, "radio_freq": 915.0, "radio_bw": 250.0,
                    "radio_sf": 10, "radio_cr": 5, "tx_power": 20,
                    "channels": [{"index": 0, "name": "Alerts", "hash": "ab"}]}

        async def set_device_name(self, name):
            self.calls.append(("name", name))

        async def rename_device_channel(self, index, name):
            self.calls.append(("channel", index, name))

    app = FastAPI()
    app.include_router(router)
    app.state.db = Database()
    app.state.tx = Radio()
    return TestClient(app), app.state.tx


def test_companion_page_is_read_only_by_default(monkeypatch):
    monkeypatch.delenv("WX_ECHO_DEVICE_WRITES_ENABLED", raising=False)
    client, radio = _web_client()
    response = client.get("/meshcore/settings")

    assert response.status_code == 200
    assert "Weather node" in response.text
    assert "Alerts" in response.text
    assert "MeshCore Settings" in response.text
    assert "channel_secret" not in response.text
    assert "Save name" not in response.text
    assert client.post("/meshcore/settings/name", data={"name": "New"}).status_code == 403
    assert client.post("/meshcore/settings/channel/0", data={"name": "Ops"}).status_code == 403
    assert radio.calls == []


def test_companion_page_shows_offline_state(monkeypatch):
    monkeypatch.delenv("WX_ECHO_DEVICE_WRITES_ENABLED", raising=False)
    client, _ = _web_client(connected=False)
    response = client.get("/meshcore/settings")

    assert response.status_code == 200
    assert "saved MeshCore radio is offline" in response.text
    assert "Save name" not in response.text


def test_companion_edits_are_guarded_and_redirect_when_enabled(monkeypatch):
    monkeypatch.setenv("WX_ECHO_DEVICE_WRITES_ENABLED", "1")
    client, radio = _web_client()
    response = client.get("/meshcore/settings")
    assert response.status_code == 200
    assert 'action="/meshcore/settings/name"' in response.text
    assert 'action="/meshcore/settings/channel/0"' in response.text

    assert client.post("/meshcore/settings/name", data={"name": "New"}, follow_redirects=False).status_code == 303
    assert client.post("/meshcore/settings/channel/0", data={"name": "Ops"}, follow_redirects=False).status_code == 303
    assert radio.calls == [("name", "New"), ("channel", 0, "Ops")]


def test_failed_device_edit_renders_error_page(monkeypatch):
    monkeypatch.setenv("WX_ECHO_DEVICE_WRITES_ENABLED", "1")
    client, radio = _web_client()

    async def invalid_name(name):
        raise ValueError("invalid device name")

    radio.set_device_name = invalid_name
    response = client.post("/meshcore/settings/name", data={"name": "bad"})
    assert response.status_code == 400
    assert "invalid device name" in response.text
    assert "MeshCore Settings" in response.text


@pytest.mark.asyncio
async def test_companion_read_waits_for_transmit_lock():
    class Database:
        def get_setting(self, key, default=None):
            return default

    tx = TransmitManager(Database())
    transport = tx._transports["meshcore"]
    transport.enabled = True
    transport.target = "/dev/ttyUSB0"
    transport.connected = True

    class Radio:
        connected = True

        async def read_settings(self):
            return {"name": "Safe"}

    transport.tx = Radio()
    async with tx._lock:
        task = asyncio.create_task(tx.get_device_settings())
        await asyncio.sleep(0)
        assert not task.done()
    assert await task == {"name": "Safe"}


@pytest.mark.asyncio
async def test_channel_rename_refreshes_saved_dropdown_labels():
    class Database:
        settings = {}

        def get_setting(self, key, default=None):
            return default

        def set_setting(self, key, value):
            self.settings[key] = value

    class Radio:
        connected = True

        async def rename_channel(self, index, name):
            assert (index, name) == (2, "Weather")
            return {"index": index, "name": name}

        async def read_channels(self):
            return [{"index": 2, "name": "Weather"}]

    db = Database()
    tx = TransmitManager(db)
    transport = tx._transports["meshcore"]
    transport.connected = True
    transport.target = "/dev/ttyUSB0"
    transport.tx = Radio()

    assert await tx.rename_device_channel(2, "Weather") == {"index": 2, "name": "Weather"}
    assert db.settings["meshcore_channels"] == [{"index": 2, "name": "Weather"}]