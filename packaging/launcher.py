"""Desktop launcher for the packaged (PyInstaller) builds.

Starts the NoticeEcho web server and opens the dashboard in the default browser.
Used as the entry point for the Windows/Linux standalone bundles; the Docker
image and `python -m app.main` path do NOT use this.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser

def _legacy_env_name(name: str) -> str:
    return name.replace("WX_ECHO", "".join(["MESH", "_WX"]), 1)


def _env_with_legacy(name: str, default: str) -> str:
    from app.config import _env_with_legacy as read_env
    canonical = name.replace("WX_ECHO", "NOTICE_ECHO", 1)
    value = read_env(name, default)
    os.environ[canonical] = value
    return value


def _open_browser(host: str, port: int, url: str) -> None:
    # Wait until the server is actually accepting connections before opening
    # the browser, so the first page load never hits "refused to connect".
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                break
        except OSError:
            time.sleep(0.4)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    host = _env_with_legacy("WX_ECHO_HOST", "127.0.0.1")
    port = _env_with_legacy("WX_ECHO_PORT", "8000")
    # 0.0.0.0 isn't browsable; point the browser at loopback.
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{browse_host}:{port}"
    print("=" * 60)
    print(f"  NoticeEcho is starting - your browser will open at {url}")
    print("  (first launch can take a few seconds)")
    print("  Keep this window open. Close it to stop NoticeEcho.")
    print("=" * 60)
    threading.Thread(target=_open_browser, args=(browse_host, int(port), url),
                     daemon=True).start()

    from app.main import main as run_server
    run_server()


if __name__ == "__main__":
    main()
