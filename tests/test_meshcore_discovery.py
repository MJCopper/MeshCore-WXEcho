from types import SimpleNamespace

import pytest

from app.meshcore_discovery import _stable_port, find_meshcore_devices


def test_stable_port_uses_by_id_symlink(tmp_path):
    device = tmp_path / "ttyUSB0"
    device.touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    link = by_id / "usb-Seeed_Studio_XIAO_nRF52840_B89FC3F98AFD92B1-if00"
    link.symlink_to(device)

    assert _stable_port(str(device), by_id) == str(link)
    assert _stable_port(str(device), tmp_path / "missing") == str(device)


@pytest.mark.asyncio
async def test_finds_meshcore_device(monkeypatch):
    import meshcore
    from serial.tools import list_ports

    port = SimpleNamespace(device="/dev/ttyUSB0", description="USB UART", vid=1, pid=2)
    monkeypatch.setattr(list_ports, "comports", lambda: [port])

    class FakeRadio:
        def __init__(self):
            self.disconnected = False
            self.commands = self

        async def send_device_query(self):
            return SimpleNamespace(
                type=meshcore.EventType.DEVICE_INFO,
                payload={"model": "Test Node", "ver": "1.2.3"},
            )

        async def disconnect(self):
            self.disconnected = True

    radio = FakeRadio()

    class FakeMeshCore:
        @staticmethod
        async def create_serial(*args, **kwargs):
            assert kwargs["default_timeout"] == 6.0
            return radio

    monkeypatch.setattr(meshcore, "MeshCore", FakeMeshCore)
    devices = await find_meshcore_devices()

    assert len(devices) == 1
    assert devices[0].port == "/dev/ttyUSB0"
    assert devices[0].model == "Test Node"
    assert radio.disconnected


@pytest.mark.asyncio
async def test_ignores_non_meshcore_device(monkeypatch):
    import meshcore
    from serial.tools import list_ports

    port = SimpleNamespace(device="/dev/ttyACM0", description="Other device")
    monkeypatch.setattr(list_ports, "comports", lambda: [port])

    class FakeRadio:
        commands = None

        async def disconnect(self):
            pass

    class FakeMeshCore:
        @staticmethod
        async def create_serial(*args, **kwargs):
            return FakeRadio()

    monkeypatch.setattr(meshcore, "MeshCore", FakeMeshCore)
    assert await find_meshcore_devices() == []


@pytest.mark.asyncio
async def test_includes_active_saved_device_without_reopening(monkeypatch):
    import meshcore
    from serial.tools import list_ports

    port = SimpleNamespace(
        device="/dev/ttyACM0",
        description="XIAO nRF52840",
        vid=0x2886,
        pid=0x0045,
    )
    monkeypatch.setattr(list_ports, "comports", lambda: [port])
    monkeypatch.setattr(
        "app.meshcore_discovery._stable_port",
        lambda device: "/dev/serial/by-id/usb-Seeed_Studio_XIAO_nRF52840_B89FC3F98AFD92B1-if00",
    )

    class FakeMeshCore:
        @staticmethod
        async def create_serial(*args, **kwargs):
            raise AssertionError("active serial port must not be reopened")

    monkeypatch.setattr(meshcore, "MeshCore", FakeMeshCore)
    devices = await find_meshcore_devices(
        active_port="/dev/serial/by-id/usb-Seeed_Studio_XIAO_nRF52840_B89FC3F98AFD92B1-if00",
        active_model="Companion",
    )

    assert len(devices) == 1
    assert devices[0].port.endswith("B89FC3F98AFD92B1-if00")
    assert devices[0].model == "Companion"


@pytest.mark.asyncio
async def test_deduplicates_serial_aliases(monkeypatch):
    import meshcore
    from serial.tools import list_ports

    ports = [
        SimpleNamespace(device="/dev/ttyACM0", description="XIAO", vid=1, pid=2),
        SimpleNamespace(device="/dev/serial-alias", description="XIAO", vid=1, pid=2),
    ]
    monkeypatch.setattr(list_ports, "comports", lambda: ports)
    monkeypatch.setattr(
        "app.meshcore_discovery._stable_port",
        lambda device: "/dev/serial/by-id/usb-Seeed_XIAO-if00",
    )

    class FakeRadio:
        commands = None

        def __init__(self):
            self.commands = self

        async def send_device_query(self):
            return SimpleNamespace(
                type=meshcore.EventType.DEVICE_INFO,
                payload={"model": "Companion", "ver": "1.0"},
            )

        async def disconnect(self):
            pass

    class FakeMeshCore:
        @staticmethod
        async def create_serial(*args, **kwargs):
            return FakeRadio()

    monkeypatch.setattr(meshcore, "MeshCore", FakeMeshCore)
    devices = await find_meshcore_devices()

    assert [device.port for device in devices] == [
        "/dev/serial/by-id/usb-Seeed_XIAO-if00"
    ]
