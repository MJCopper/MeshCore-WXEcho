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


async def find_meshcore_devices(timeout: float = 5.0,
                                probe_timeout: float = 1.5) -> list[DetectedMeshCore]:
    """Return serial ports that answer a MeshCore device-info query."""
    from serial.tools import list_ports

    ports = [port for port in list_ports.comports() if getattr(port, "device", "")]
    if not ports:
        return []
    probes = [_probe_port(port, probe_timeout) for port in ports]
    try:
        results = await asyncio.wait_for(asyncio.gather(*probes), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("MeshCore port discovery timed out after %.1fs", timeout)
        return []
    return [result for result in results if result is not None]