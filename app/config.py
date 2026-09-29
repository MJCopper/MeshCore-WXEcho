"""Bootstrap configuration from environment variables.

Only the values needed to *start* the app live here (HTTP port, db path).
Everything else is stored in the database and editable in the UI.
"""
from __future__ import annotations

import os
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

APP_DIRNAME = "WXEcho"
LEGACY_APP_DIRNAME = "".join(["Mesh", "WX"])
LEGACY_LINUX_DIRNAME = "".join(["mesh", "-wx"])
LEGACY_DB_NAME = "".join(["mesh", "-wx", ".db"])


def _legacy_env_name(name: str) -> str:
    return name.replace("WX_ECHO", "".join(["MESH", "_WX"]), 1)


def _env_with_legacy(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is not None:
        return val
    legacy = _legacy_env_name(name)
    legacy_val = os.environ.get(legacy)
    if legacy_val is not None:
        warnings.warn(
            f"{legacy} is deprecated; use {name} instead.",
            RuntimeWarning,
            stacklevel=2,
        )
        return legacy_val
    return default


def device_writes_enabled() -> bool:
    return os.environ.get("WX_ECHO_DEVICE_WRITES_ENABLED", "").lower() in {"1", "true", "yes", "on"}


def default_data_dir() -> Path:
    """Per-OS location for the database and other runtime state.

    Chosen so the app runs unprivileged out-of-the-box on every platform:
      * Docker/Linux containers .......... /data   (if it exists and is writable)
      * Windows .......................... %LOCALAPPDATA%\\WXEcho
      * macOS ............................ ~/Library/Application Support/WXEcho
      * Linux/Raspberry Pi (native) ...... $XDG_DATA_HOME/wx-echo  (~/.local/share/wx-echo)
    """
    # Honour the container convention when /data is mounted.
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return Path("/data")

    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
            or str(Path.home() / "AppData" / "Local")
        new_dir = Path(base) / APP_DIRNAME
        legacy_dir = Path(base) / LEGACY_APP_DIRNAME
        if legacy_dir.exists() and not new_dir.exists():
            warnings.warn(
                f"Using legacy data directory '{legacy_dir}'. Move to '{new_dir}' when convenient.",
                RuntimeWarning,
                stacklevel=2,
            )
            return legacy_dir
        return new_dir
    if sys.platform == "darwin":
        new_dir = Path.home() / "Library" / "Application Support" / APP_DIRNAME
        legacy_dir = Path.home() / "Library" / "Application Support" / LEGACY_APP_DIRNAME
        if legacy_dir.exists() and not new_dir.exists():
            warnings.warn(
                f"Using legacy data directory '{legacy_dir}'. Move to '{new_dir}' when convenient.",
                RuntimeWarning,
                stacklevel=2,
            )
            return legacy_dir
        return new_dir
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    new_dir = Path(base) / "wx-echo"
    legacy_dir = Path(base) / LEGACY_LINUX_DIRNAME
    if legacy_dir.exists() and not new_dir.exists():
        warnings.warn(
            f"Using legacy data directory '{legacy_dir}'. Move to '{new_dir}' when convenient.",
            RuntimeWarning,
            stacklevel=2,
        )
        return legacy_dir
    return new_dir


@dataclass(frozen=True)
class BootstrapConfig:
    http_host: str
    http_port: int
    db_path: str


def load_bootstrap() -> BootstrapConfig:
    db_path = _env_with_legacy("WX_ECHO_DB")
    if not db_path:
        data_dir = default_data_dir()
        new_db = data_dir / "wx-echo.db"
        legacy_db = data_dir / LEGACY_DB_NAME
        if legacy_db.exists() and not new_db.exists():
            warnings.warn(
                f"Using legacy database path '{legacy_db}'. Move to '{new_db}' when convenient.",
                RuntimeWarning,
                stacklevel=2,
            )
            db_path = str(legacy_db)
        else:
            db_path = str(new_db)
    # Make sure the parent directory exists so SQLite can create the file.
    parent = Path(db_path).expanduser().parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return BootstrapConfig(
        http_host=_env_with_legacy("WX_ECHO_HOST", "0.0.0.0") or "0.0.0.0",
        http_port=int(_env_with_legacy("WX_ECHO_PORT", "8000") or "8000"),
        db_path=str(Path(db_path).expanduser()),
    )


# Default settings seeded into the db on first run. Every one of these is
# editable in the UI afterwards; env vars never override stored settings.
DEFAULT_SETTINGS: dict = {
    "bom_regions": ["NSW"],
    "bom_districts": [],
    "poll_interval": 120,
    "bom_contact": "MeshCore BOM Weather (change-me@example.com)",
    "meshcore_enabled": True,
    "meshcore_conn": "serial",
    "meshcore_port": "",
    "meshcore_host": "",
    "meshcore_channel": 0,
    "meshcore_repeat": 2,
    "meshcore_test_channel": 1,
    "dry_run": True,
    "test_channel": 1,
    "display_timezone": "Australia/Sydney",
    # Filter rules (editable). An alert is INCLUDED when its event is in
    # filter_include_exact OR ends with any suffix in filter_include_suffix,
    # UNLESS the event is in filter_exclude_exact.
    "filter_include_exact": [],
    "filter_include_suffix": ["Warning"],
    "filter_exclude_exact": [],
}

POLL_INTERVAL_MIN = 60
# Hard ceiling on a single poll cycle. The BOM fetch is already bounded (30s
# timeout x a few retries), so exceeding this means something hung (DB lock,
# wedged await, a bug). The watchdog aborts the poll so the loop always recovers.
POLL_HARD_TIMEOUT = 180
MAX_PAYLOAD_BYTES = 195
FINAL_VERIFICATION_MESSAGE = (
    "UNOFFICIAL automated relay. May be incomplete or inaccurate. Verify warnings at "
    "bom.gov.au/weather-and-climate/warnings-and-alerts"
)
if len(FINAL_VERIFICATION_MESSAGE.encode("utf-8")) > MAX_PAYLOAD_BYTES:
    raise ValueError("FINAL_VERIFICATION_MESSAGE exceeds MAX_PAYLOAD_BYTES")

BURST_GAP_SECONDS = 30
MULTIPART_GAP_SECONDS = 3

REPEAT_GAP_SECONDS = 5   # gap between repeated copies of the same alert
QUEUE_MAX = 20
STATE_EXPIRY_HOURS = 48
