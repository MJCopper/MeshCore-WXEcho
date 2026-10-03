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

APP_DIRNAME = "NoticeEcho"
LEGACY_APP_DIRNAME = "".join(["Mesh", "WX"])
LEGACY_LINUX_DIRNAME = "".join(["mesh", "-wx"])
LEGACY_DB_NAME = "".join(["mesh", "-wx", ".db"])


def _legacy_env_name(name: str) -> str:
    return name.replace("WX_ECHO", "".join(["MESH", "_WX"]), 1)


def _env_with_legacy(name: str, default: str | None = None) -> str | None:
    """NoticeEcho variables take precedence; both older prefixes remain accepted."""
    canonical = name.replace("WX_ECHO", "NOTICE_ECHO", 1)
    for candidate in (canonical, name, _legacy_env_name(name)):
        if candidate in os.environ:
            if candidate == _legacy_env_name(name):
                warnings.warn(f"{candidate} is deprecated; use {canonical} instead.",
                              RuntimeWarning, stacklevel=2)
            return os.environ[candidate]
    return default


def default_data_dir() -> Path:
    """Choose a new NoticeEcho directory, reusing existing installation storage."""
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return Path("/data")
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                    or str(Path.home() / "AppData" / "Local"))
        candidates = [base / "NoticeEcho", base / "WXEcho", base / LEGACY_APP_DIRNAME]
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        candidates = [base / "NoticeEcho", base / "WXEcho", base / LEGACY_APP_DIRNAME]
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share"))
        candidates = [base / "notice-echo", base / "wx-echo", base / LEGACY_LINUX_DIRNAME]
    return next((path for path in candidates if path.exists()), candidates[0])


@dataclass(frozen=True)
class BootstrapConfig:
    http_host: str
    http_port: int
    db_path: str


def load_bootstrap() -> BootstrapConfig:
    db_path = _env_with_legacy("WX_ECHO_DB")
    if not db_path:
        data_dir = default_data_dir()
        candidates = [data_dir / "notice-echo.db", data_dir / "wx-echo.db", data_dir / LEGACY_DB_NAME]
        db_path = str(next((path for path in candidates if path.is_file()), candidates[0]))
    # Make sure the parent directory exists so SQLite can create the file.
    parent = Path(db_path).expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)
    return BootstrapConfig(
        http_host=_env_with_legacy("WX_ECHO_HOST", "0.0.0.0") or "0.0.0.0",
        http_port=int(_env_with_legacy("WX_ECHO_PORT", "8000") or "8000"),
        db_path=str(Path(db_path).expanduser()),
    )


# Default settings seeded into the db on first run. Environment variables do
# not override stored settings.
# Use one browser-style User-Agent for all BOM requests.
BOM_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

DEFAULT_SETTINGS: dict = {
    "bom_enabled": True,
    "bom_all_councils": True,
    "bom_councils": [],
    "bom_include_unknown_councils": True,
    "bom_districts": [],
    "rfs_enabled": False,
    "rfs_all_councils": False,
    "rfs_councils": [],
    "rfs_levels": ["Emergency Warning", "Watch and Act"],
    "traffic_enabled": False,
    "traffic_all_councils": False,
    "traffic_councils": [],
    "traffic_types": ["incident", "flood", "regional"],
    "poll_interval": 300,  # legacy seconds; BOM now uses bom_poll_minutes
    "bom_poll_minutes": 5,
    "rfs_poll_minutes": 10,
    "traffic_poll_minutes": 10,
    "meshcore_enabled": True,
    "meshcore_conn": "serial",
    "meshcore_port": "",
    "meshcore_host": "",
    "meshcore_channel": 0,
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

MIN_POLL_MINUTES = 5
POLL_INTERVAL_MIN = MIN_POLL_MINUTES * 60
# Hard ceiling on a single poll cycle. The BOM fetch is already bounded (30s
# timeout x a few retries), so exceeding this means something hung (DB lock,
# wedged await, a bug). The watchdog aborts the poll so the loop always recovers.
POLL_HARD_TIMEOUT = 180
MESHCORE_CHANNEL_TEXT_BYTES = 160
MESHCORE_MAX_NAME_BYTES = 32
# Safe while the companion is offline; the live budget uses its actual name.
MAX_PAYLOAD_BYTES = MESHCORE_CHANNEL_TEXT_BYTES - MESHCORE_MAX_NAME_BYTES - 2
FINAL_VERIFICATION_MESSAGE = (
    "UNOFFICIAL relay. May be incorrect or incomplete. Verify independently. "
    "Never base safety decisions on these notices."
)

VERIFICATION_INTERVAL_SECONDS = 300

BURST_GAP_SECONDS = 30
MULTIPART_GAP_SECONDS = 3

REPEAT_GAP_SECONDS = 5   # gap between repeated copies of the same alert
QUEUE_MAX = 20
QUEUE_BYTE_MAX = 1024 * 1024  # pending notice text; full notices defer above this bound
STATE_EXPIRY_HOURS = 48


def polling_seconds(value: object, default_minutes: int) -> int:
    """Convert a persisted service interval to seconds with the common floor."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = default_minutes
    return max(MIN_POLL_MINUTES, minutes) * 60
