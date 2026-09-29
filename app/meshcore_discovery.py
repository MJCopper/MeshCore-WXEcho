"""List USB serial paths exposed by Linux in /dev/serial/by-id/."""
from __future__ import annotations

from pathlib import Path


def list_usb_serial_devices(by_id_dir: Path = Path("/dev/serial/by-id")) -> list[str]:
    """Return every directory entry as a selectable, stable serial path."""
    try:
        return [str(path) for path in sorted(by_id_dir.iterdir())]
    except FileNotFoundError:
        return []
