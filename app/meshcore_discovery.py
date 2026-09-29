"""Discover MeshCore devices connected through USB serial ports."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("wx_echo.discovery")


@dataclass
class DetectedMeshCore:
    port: str
    description: str
    model: str
    firmware: str
    vid: int | None = None
    pid: int | None = None


def _stable_port(device: str, by_id_dir: Path = Path("/dev/serial/by-id")) -> str:
    target = Path(device).resolve()
    if by_id_dir.is_dir():
        for path in sorted(by_id_dir.iterdir()):
            if path.is_symlink() and path.resolve() == target:
                return str(path)
    return device


def _same_port(first: str, second: str) -> bool:
    if not first or not second:
        return False
    return Path(first).resolve() == Path(second).resolve()


async def _probe_port(port_info, timeout: float) -> DetectedMeshCore | None:
    from meshcore import EventType, MeshCore

    meshcore = None
    try:
        meshcore = await asyncio.wait_for(
            MeshCore.create_serial(
                port_info.device,
                baud=115200,
                default_timeout=timeout,
                auto_reconnect=False,
            ),
            timeout=timeout,
        )
        if meshcore is None:
            return None
        result = await asyncio.wait_for(
            meshcore.commands.send_device_query(), timeout=timeout
        )
        if getattr(result, "type", None) != EventType.DEVICE_INFO:
            return None
        payload = getattr(result, "payload", {}) or {}
        return DetectedMeshCore(
            port=_stable_port(port_info.device),
            description=(getattr(port_info, "description", "") or "").strip(),
            model=(payload.get("model") or "").strip(),
            firmware=(payload.get("ver") or "").strip(),
            vid=getattr(port_info, "vid", None),
            pid=getattr(port_info, "pid", None),
        )
    except asyncio.TimeoutError:
        logger.debug("MeshCore probe timed out on %s", port_info.device)
    except Exception as exc:
        logger.debug("MeshCore probe failed on %s: %s", port_info.device, exc)
    finally:
        if meshcore is not None:
            try:
                await asyncio.wait_for(meshcore.disconnect(), timeout=1.0)
            except Exception:
                pass
    return None


async def find_meshcore_devices(
    timeout: float = 14.0,
    probe_timeout: float = 6.0,
    active_port: str = "",
    active_model: str = "",
    active_firmware: str = "",
) -> list[DetectedMeshCore]:
    """Return serial ports that answer a MeshCore device-info query."""
    from serial.tools import list_ports

    ports = [port for port in list_ports.comports() if getattr(port, "device", "")]
    if not ports:
        return []

    devices = []
    ports_to_probe = []
    for port in ports:
        stable_port = _stable_port(port.device)
        if _same_port(port.device, active_port) or _same_port(stable_port, active_port):
            devices.append(DetectedMeshCore(
                port=stable_port,
                description=(getattr(port, "description", "") or "").strip(),
                model=active_model.strip(),
                firmware=active_firmware.strip(),
                vid=getattr(port, "vid", None),
                pid=getattr(port, "pid", None),
            ))
        else:
            ports_to_probe.append(port)

    probes = [_probe_port(port, probe_timeout) for port in ports_to_probe]
    try:
        results = await asyncio.wait_for(asyncio.gather(*probes), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("MeshCore port discovery timed out after %.1fs", timeout)
        results = []

    devices.extend(result for result in results if result is not None)
    unique = {}
    for device in devices:
        unique[str(Path(device.port).resolve())] = device
    return sorted(unique.values(), key=lambda device: device.port)