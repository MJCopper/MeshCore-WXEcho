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
    with pytest.raises(ValueError, match="Confirm the key change"):
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


@pytest.mark.asyncio
async def test_tx_power_write_checks_device_limit_and_readback():
    class Commands:
        power = 20
        writes = []

        async def send_appstart(self):
            return SimpleNamespace(type=EventType.SELF_INFO,
                                   payload={"max_tx_power": 22, "tx_power": self.power})

        async def set_tx_power(self, power):
            self.writes.append(power)
            self.power = power
            return SimpleNamespace(type=EventType.OK)

    commands = Commands()
    transmitter = MeshCoreTransmitter("serial")
    transmitter._mc = SimpleNamespace(is_connected=True, commands=commands)

    with pytest.raises(ValueError, match="between 0 and 22"):
        await transmitter.set_tx_power(23)
    assert commands.writes == []
    assert await transmitter.set_tx_power(21) == 21
    assert commands.writes == [21]

    async def stale_readback():
        return SimpleNamespace(type=EventType.SELF_INFO,
                               payload={"max_tx_power": 22, "tx_power": 20})
    commands.send_appstart = stale_readback
    with pytest.raises(RuntimeError, match="verify"):
        await transmitter.set_tx_power(19)


@pytest.mark.asyncio
async def test_radio_parameter_write_validates_and_verifies_readback():
    class Commands:
        values = {"radio_freq": 915.0, "radio_bw": 250.0,
                  "radio_sf": 10, "radio_cr": 5}
        writes = []

        async def set_radio(self, freq, bw, sf, cr):
            self.writes.append((freq, bw, sf, cr))
            self.values = {"radio_freq": freq, "radio_bw": bw,
                           "radio_sf": sf, "radio_cr": cr}
            return SimpleNamespace(type=EventType.OK)

        async def send_appstart(self):
            return SimpleNamespace(type=EventType.SELF_INFO, payload=self.values)

    commands = Commands()
    transmitter = MeshCoreTransmitter("serial")
    transmitter._mc = SimpleNamespace(is_connected=True, commands=commands)

    with pytest.raises(ValueError, match="spreading factor"):
        await transmitter.set_radio_parameters(915.0, 250.0, 13, 5)
    assert commands.writes == []
    saved = await transmitter.set_radio_parameters(916.0, 125.0, 9, 6)
    assert saved == {"radio_freq": 916.0, "radio_bw": 125.0,
                     "radio_sf": 9, "radio_cr": 6}
    assert commands.writes == [(916.0, 125.0, 9, 6)]

    async def stale_readback():
        return SimpleNamespace(type=EventType.SELF_INFO,
                               payload={"radio_freq": 915.0, "radio_bw": 250.0,
                                        "radio_sf": 10, "radio_cr": 5})
    commands.send_appstart = stale_readback
    with pytest.raises(RuntimeError, match="did not match"):
        await transmitter.set_radio_parameters(916.0, 125.0, 9, 6)


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
                    "max_tx_power": 22,
                    "channels": [{"index": 0, "name": "Alerts", "hash": "ab"}]}

        async def set_device_name(self, name):
            self.calls.append(("name", name))

        async def rename_device_channel(self, index, name):
            self.calls.append(("channel", index, name))

        async def set_tx_power(self, power):
            self.calls.append(("power", power))

        async def set_radio_parameters(self, freq, bw, sf, cr):
            self.calls.append(("radio", freq, bw, sf, cr))

    app = FastAPI()
    app.include_router(router)
    app.state.db = Database()
    app.state.tx = Radio()
    return TestClient(app), app.state.tx


def test_companion_page_is_editable():
    client, radio = _web_client()
    response = client.get("/meshcore/settings")

    assert response.status_code == 200
    assert "Weather node" in response.text
    assert "Alerts" in response.text
    assert "channel_secret" not in response.text
    assert 'action="/meshcore/settings/name"' in response.text
    assert 'action="/meshcore/settings/channel/0"' in response.text
    assert 'action="/meshcore/settings/tx-power"' in response.text
    assert 'action="/meshcore/settings/radio"' in response.text
    assert client.post("/meshcore/settings/name", data={"name": "New"}, follow_redirects=False).status_code == 303
    assert client.post("/meshcore/settings/channel/0", data={"name": "Ops"}, follow_redirects=False).status_code == 303
    assert radio.calls == [("name", "New"), ("channel", 0, "Ops")]


def test_companion_page_shows_offline_state():
    client, _ = _web_client(connected=False)
    response = client.get("/meshcore/settings")

    assert response.status_code == 200
    assert "saved MeshCore radio is offline" in response.text
    assert "Save name" not in response.text


def test_companion_radio_edits_redirect_after_save():
    client, radio = _web_client()
    response = client.get("/meshcore/settings")
    assert response.status_code == 200
    assert 'action="/meshcore/settings/name"' in response.text
    assert 'action="/meshcore/settings/channel/0"' in response.text
    assert 'action="/meshcore/settings/tx-power"' in response.text
    assert 'action="/meshcore/settings/radio"' in response.text

    assert client.post("/meshcore/settings/name", data={"name": "New"}, follow_redirects=False).status_code == 303
    assert client.post("/meshcore/settings/channel/0", data={"name": "Ops"}, follow_redirects=False).status_code == 303
    assert client.post("/meshcore/settings/tx-power", data={"power": "21"}, follow_redirects=False).status_code == 303
    assert client.post("/meshcore/settings/radio", data={"freq": "915", "bw": "250", "sf": "10", "cr": "5"}, follow_redirects=False).status_code == 303
    assert radio.calls == [("name", "New"), ("channel", 0, "Ops"),
                           ("power", 21), ("radio", 915.0, 250.0, 10, 5)]


def test_failed_device_edit_renders_error_page():
    client, radio = _web_client()

    async def invalid_name(name):
        raise ValueError("invalid device name")

    radio.set_device_name = invalid_name
    response = client.post("/meshcore/settings/name", data={"name": "bad"})
    assert response.status_code == 400
    assert "invalid device name" in response.text
    assert "MeshCore Settings" in response.text
    assert 'action="/meshcore/settings/name"' in response.text


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
@pytest.mark.asyncio
async def test_load_channels_reads_active_radio_without_reopening():
    class Database:
        def get_setting(self, key, default=None):
            return default

    class Radio:
        connected = True

        async def read_channels(self):
            return [{"index": 2, "name": "Weather"}]

        async def read_info(self):
            return {"model": "Companion", "firmware": "1.0"}

        async def close(self):
            raise AssertionError("active connection must stay open")

    tx = TransmitManager(Database())
    transport = tx._transports["meshcore"]
    transport.target = "/dev/ttyACM0"
    transport.connected = True
    transport.tx = Radio()

    channels, model, error = await tx.load_channels(
        "meshcore", "serial", "/dev/ttyACM0", ""
    )

    assert channels == [{"index": 2, "name": "Weather"}]
    assert model == "Companion (fw 1.0)"
    assert error == ""
    assert transport.tx.connected


@pytest.mark.asyncio
async def test_load_channels_new_target_keeps_active_radio_connected(monkeypatch):
    class Database:
        def get_setting(self, key, default=None):
            return default

    class ActiveRadio:
        connected = True

        async def close(self):
            raise AssertionError("active connection must stay open")

    class TemporaryRadio:
        def __init__(self, conn, port, host):
            assert (conn, port, host) == ("serial", "/dev/ttyACM1", "")
            self.closed = False

        async def connect(self):
            pass

        async def read_channels(self):
            return [{"index": 3, "name": "New radio"}]

        async def read_info(self):
            return {}

        async def close(self):
            self.closed = True

    temporary = []
    def make_temporary(*args):
        radio = TemporaryRadio(*args)
        temporary.append(radio)
        return radio

    monkeypatch.setattr("app.transmit.MeshCoreTransmitter", make_temporary)
    tx = TransmitManager(Database())
    transport = tx._transports["meshcore"]
    transport.target = "/dev/ttyACM0"
    transport.connected = True
    active = transport.tx = ActiveRadio()

    channels, _, error = await tx.load_channels(
        "meshcore", "serial", "/dev/ttyACM1", ""
    )

    assert channels == [{"index": 3, "name": "New radio"}]
    assert error == ""
    assert transport.tx is active
    assert temporary[0].closed


def test_legacy_disabled_setting_does_not_disable_meshcore():
    class Database:
        def get_setting(self, key, default=None):
            return {"meshcore_enabled": False}.get(key, default)

    assert TransmitManager(Database()).status()[0]["enabled"] is True


@pytest.mark.asyncio
async def test_add_private_channel_uses_first_empty_slot_and_verifies_key(monkeypatch):
    class Commands:
        slots = {
            0: ("Public", b"p" * 16),
            1: ("", bytes(16)),
            2: ("Existing", b"e" * 16),
        }
        writes = []

        async def send_device_query(self):
            return SimpleNamespace(type=EventType.DEVICE_INFO, payload={"max_channels": 3})

        async def get_channel(self, index):
            name, secret = self.slots[index]
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_name": name, "channel_secret": secret,
            })

        async def set_channel(self, index, name, secret):
            self.writes.append((index, name, secret))
            self.slots[index] = (name, secret)
            return SimpleNamespace(type=EventType.OK)

    commands = Commands()
    transmitter = MeshCoreTransmitter("serial")
    transmitter._mc = SimpleNamespace(is_connected=True, commands=commands)
    monkeypatch.setattr("app.transmit.secrets.token_bytes", lambda count: b"g" * count)

    created = await transmitter.add_channel("Weather")
    assert created == {"index": 1, "name": "Weather", "secret_hex": (b"g" * 16).hex()}
    assert commands.writes == [(1, "Weather", b"g" * 16)]
    with pytest.raises(RuntimeError, match="no empty private"):
        await transmitter.add_channel("Another")


@pytest.mark.asyncio
async def test_add_channel_accepts_existing_key_and_rejects_invalid_key():
    class Commands:
        async def send_device_query(self):
            return SimpleNamespace(type=EventType.DEVICE_INFO, payload={"max_channels": 2})

        async def get_channel(self, index):
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_name": "Public" if index == 0 else self.name,
                "channel_secret": b"p" * 16 if index == 0 else self.secret,
            })

        async def set_channel(self, index, name, secret):
            self.name, self.secret = name, secret
            return SimpleNamespace(type=EventType.OK)

        name = ""
        secret = bytes(16)

    transmitter = MeshCoreTransmitter("serial")
    commands = Commands()
    transmitter._mc = SimpleNamespace(is_connected=True, commands=commands)
    with pytest.raises(ValueError, match="32 hexadecimal"):
        await transmitter.add_channel("Weather", "not-a-key")
    with pytest.raises(ValueError, match="all zeros"):
        await transmitter.add_channel("Weather", "00" * 16)
    assert commands.name == ""
    result = await transmitter.add_channel("Weather", "ab" * 16)
    assert result == {"index": 1, "name": "Weather", "secret_hex": ""}
    assert commands.secret == bytes.fromhex("ab" * 16)


@pytest.mark.asyncio
async def test_remove_channel_clears_secret_and_protects_public_slot():
    class Commands:
        name = "Weather"
        secret = b"w" * 16
        calls = []

        async def get_channel(self, index):
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_name": self.name, "channel_secret": self.secret,
            })

        async def set_channel(self, index, name, secret):
            self.calls.append((index, name, secret))
            self.name, self.secret = name, secret
            return SimpleNamespace(type=EventType.OK)

    commands = Commands()
    transmitter = MeshCoreTransmitter("serial")
    transmitter._mc = SimpleNamespace(is_connected=True, commands=commands)
    with pytest.raises(ValueError, match="slots 1-255"):
        await transmitter.remove_channel(0)
    await transmitter.remove_channel(2)
    assert commands.calls == [(2, "", bytes(16))]
    with pytest.raises(RuntimeError, match="unavailable"):
        await transmitter.remove_channel(2)


@pytest.mark.asyncio
async def test_manager_blocks_removal_of_live_or_test_channel():
    class Database:
        def get_setting(self, key, default=None):
            return {"meshcore_channel": 2, "meshcore_test_channel": 3}.get(key, default)

    class Radio:
        connected = True
        calls = []

        async def remove_channel(self, index):
            self.calls.append(index)

    tx = TransmitManager(Database())
    transport = tx._transports["meshcore"]
    transport.target = "/dev/ttyUSB0"
    transport.connected = True
    transport.tx = Radio()
    for index in (0, 2, 3):
        with pytest.raises(ValueError):
            await tx.remove_device_channel(index)
    assert transport.tx.calls == []


def test_channel_management_page_and_generated_key_response():
    client, radio = _web_client()

    async def settings():
        return {"name": "Weather node", "model": "Companion", "firmware": "1.4",
                "battery_mv": None, "radio_freq": None, "radio_bw": None,
                "radio_sf": None, "radio_cr": None, "tx_power": None,
                "max_tx_power": None, "available_slots": [1, 2],
                "channels": [{"index": 0, "name": "Public", "hash": "ab"},
                             {"index": 3, "name": "Ops", "hash": "cd"}]}

    async def add(name, secret_hex):
        radio.calls.append(("add", name, secret_hex))
        return {"index": 1, "name": name, "secret_hex": "ab" * 16}

    async def remove(index):
        radio.calls.append(("remove", index))

    radio.get_device_settings = settings
    radio.add_device_channel = add
    radio.remove_device_channel = remove
    page = client.get("/meshcore/settings")
    assert 'action="/meshcore/settings/channels/add"' in page.text
    assert 'action="/meshcore/settings/channel/3/remove"' in page.text
    assert 'action="/meshcore/settings/channel/0/remove"' not in page.text
    assert "ab" * 16 not in page.text

    created = client.post("/meshcore/settings/channels/add",
                          data={"name": "Weather", "secret_hex": ""})
    assert created.status_code == 200
    assert "ab" * 16 in created.text
    assert created.headers["cache-control"] == "no-store"
    assert "ab" * 16 not in client.get("/meshcore/settings").text
    removed = client.post("/meshcore/settings/channel/3/remove", follow_redirects=False)
    assert removed.status_code == 303
    assert radio.calls == [("add", "Weather", ""), ("remove", 3)]


def test_channel_management_errors_are_rendered():
    client, radio = _web_client()

    async def invalid(name, secret_hex):
        raise ValueError("channel key must be exactly 32 hexadecimal characters")

    radio.add_device_channel = invalid
    response = client.post("/meshcore/settings/channels/add",
                           data={"name": "Weather", "secret_hex": "wrong"})
    assert response.status_code == 400
    assert "channel key must be exactly 32 hexadecimal characters" in response.text


@pytest.mark.asyncio
async def test_channel_snapshot_identifies_only_verified_empty_private_slots():
    class Commands:
        async def send_device_query(self):
            return SimpleNamespace(type=EventType.DEVICE_INFO, payload={"max_channels": 4})

        async def send_appstart(self):
            return SimpleNamespace(type=EventType.SELF_INFO, payload={})

        async def get_bat(self):
            return SimpleNamespace(type=EventType.ERROR)

        async def get_channel(self, index):
            names = ["Public", "", "Old", ""]
            secrets = [b"p" * 16, bytes(16), b"o" * 16, b"z" * 16]
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_name": names[index], "channel_secret": secrets[index],
            })

    transmitter = MeshCoreTransmitter("serial")
    transmitter._mc = SimpleNamespace(is_connected=True, commands=Commands())
    snapshot = await transmitter.read_settings()
    assert snapshot["max_channels"] == 4
    assert snapshot["available_slots"] == [1]
    assert [channel["index"] for channel in snapshot["channels"]] == [0, 2]


@pytest.mark.asyncio
async def test_channel_add_rejects_failed_readback():
    class Commands:
        async def send_device_query(self):
            return SimpleNamespace(type=EventType.DEVICE_INFO, payload={"max_channels": 2})

        async def get_channel(self, index):
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_name": "Public" if index == 0 else "",
                "channel_secret": b"p" * 16 if index == 0 else bytes(16),
            })

        async def set_channel(self, index, name, secret):
            return SimpleNamespace(type=EventType.OK)

    transmitter = MeshCoreTransmitter("serial")
    transmitter._mc = SimpleNamespace(is_connected=True, commands=Commands())
    with pytest.raises(RuntimeError, match="verify"):
        await transmitter.add_channel("Weather", "ab" * 16)


@pytest.mark.asyncio
async def test_channel_add_and_remove_refresh_saved_dropdown_names():
    class Database:
        def __init__(self):
            self.settings = {"meshcore_channel": 0, "meshcore_test_channel": 1}

        def get_setting(self, key, default=None):
            return self.settings.get(key, default)

        def set_setting(self, key, value):
            self.settings[key] = value

    class Radio:
        connected = True
        channels = []

        async def add_channel(self, name, secret_hex):
            self.channels = [{"index": 2, "name": name}]
            return {"index": 2, "name": name, "secret_hex": "ab" * 16}

        async def remove_channel(self, index):
            self.channels = []

        async def read_channels(self):
            return self.channels

    db = Database()
    tx = TransmitManager(db)
    transport = tx._transports["meshcore"]
    transport.target = "/dev/ttyUSB0"
    transport.connected = True
    transport.tx = Radio()
    created = await tx.add_device_channel("Weather")
    assert created["secret_hex"] == "ab" * 16
    assert db.settings["meshcore_channels"] == [{"index": 2, "name": "Weather"}]
    await tx.remove_device_channel(2)
    assert db.settings["meshcore_channels"] == []
    assert db.settings["meshcore_channel"] == 0
    assert db.settings["meshcore_test_channel"] == 1


@pytest.mark.asyncio
async def test_generated_key_survives_cache_refresh_failure():
    class Database:
        def get_setting(self, key, default=None):
            return default

    class Radio:
        connected = True

        async def add_channel(self, name, secret_hex):
            return {"index": 2, "name": name, "secret_hex": "ab" * 16}

        async def read_channels(self):
            raise RuntimeError("temporary read failure")

    tx = TransmitManager(Database())
    transport = tx._transports["meshcore"]
    transport.target = "/dev/ttyUSB0"
    transport.connected = True
    transport.tx = Radio()
    created = await tx.add_device_channel("Weather")
    assert created["secret_hex"] == "ab" * 16
    assert created["refresh_error"] == "temporary read failure"


@pytest.mark.asyncio
async def test_hash_channel_add_and_rename_require_expected_derived_key():
    from hashlib import sha256
    class Commands:
        slots = {0: ("Public", b"p" * 16), 1: ("", bytes(16))}
        async def send_device_query(self):
            return SimpleNamespace(type=EventType.DEVICE_INFO, payload={"max_channels": 2})
        async def get_channel(self, index):
            name, secret = self.slots[index]
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_idx": index, "channel_name": name, "channel_secret": secret})
        async def set_channel(self, index, name, secret):
            self.slots[index] = name, secret
            return SimpleNamespace(type=EventType.OK, payload={})
    radio = MeshCoreTransmitter("serial")
    commands = Commands()
    radio._mc = SimpleNamespace(is_connected=True, commands=commands)
    result = await radio.add_channel("#weather")
    assert result["secret_hex"] == ""
    assert commands.slots[1][1] == sha256(b"#weather").digest()[:16]
    with pytest.raises(ValueError, match="Confirm"):
        await radio.rename_channel(1, "#alerts")
    assert commands.slots[1][0] == "#weather"
    await radio.rename_channel(1, "#alerts", True)
    assert commands.slots[1] == ("#alerts", sha256(b"#alerts").digest()[:16])
    await radio.rename_channel(1, "Weather")
    assert commands.slots[1][1] == sha256(b"#alerts").digest()[:16]


@pytest.mark.asyncio
async def test_channels_above_seven_are_discovered_and_created():
    class Commands:
        names = {}
        secrets = {}
        async def send_device_query(self):
            return SimpleNamespace(type=EventType.DEVICE_INFO, payload={"max_channels": 40})
        async def get_channel(self, index):
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_idx": index, "channel_name": self.names.get(index, "Taken" if index < 9 else ""),
                "channel_secret": self.secrets.get(index, b"t" * 16 if index < 9 else bytes(16))})
        async def set_channel(self, index, name, secret):
            self.names[index], self.secrets[index] = name, secret
            return SimpleNamespace(type=EventType.OK)
    radio = MeshCoreTransmitter("serial")
    radio._mc = SimpleNamespace(is_connected=True, commands=Commands())
    created = await radio.add_channel("Weather")
    assert created["index"] == 9
    channels = await radio.read_channels()
    assert {"index": 9, "name": "Weather"} in channels
    await radio.rename_channel(9, "Alerts")
    await radio.remove_channel(9)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["wrong-slot", "timeout", "rejected"])
async def test_channel_failures_identify_stage_without_keys(failure):
    class Commands:
        async def get_channel(self, index):
            if failure == "timeout":
                raise TimeoutError()
            return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={
                "channel_idx": index + (failure == "wrong-slot"), "channel_name": "Ops",
                "channel_secret": b"x" * 16})
        async def set_channel(self, index, name, secret):
            return SimpleNamespace(type=EventType.ERROR, payload={
                "code": 7, "channel_secret": "never-display-this"})
    radio = MeshCoreTransmitter("serial")
    radio._mc = SimpleNamespace(is_connected=True, commands=Commands())
    with pytest.raises(RuntimeError) as error:
        await radio.rename_channel(1, "Alerts")
    assert "never-display-this" not in str(error.value)
    assert ("different channel slot" if failure == "wrong-slot" else
            "communication interrupted" if failure == "timeout" else "code=7") in str(error.value)


@pytest.mark.asyncio
async def test_verified_rename_survives_dropdown_refresh_failure():
    class Database:
        def get_setting(self, key, default=None):
            return default
    class Radio:
        connected = True
        async def rename_channel(self, index, name):
            return {"index": index, "name": name}
        async def read_channels(self):
            raise RuntimeError("offline")
    tx = TransmitManager(Database())
    transport = tx._transports["meshcore"]
    transport.target = "/dev/ttyUSB0"
    transport.connected = True
    transport.tx = Radio()
    result = await tx.rename_device_channel(2, "Weather")
    assert result["name"] == "Weather"
    assert "saved and verified" in result["refresh_error"]
