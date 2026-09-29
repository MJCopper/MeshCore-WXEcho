# MeshCore BOM Weather

MeshCore BOM Weather polls official Australian Bureau of Meteorology warning RSS feeds and broadcasts selected warnings over a MeshCore radio. It is a small self-hosted web app for a Raspberry Pi, Linux host, Windows machine, or Docker.

> This is a supplemental warning broadcaster, not a certified warning system. Keep official BOM channels and other local emergency sources available. Test in dry-run mode before enabling live broadcasts.

## Features

- State and territory BOM warning feeds.
- Optional forecast-district filtering.
- Configurable Australian IANA timezone display.
- MeshCore over USB serial or TCP.
- BOM warning filtering, update/cancellation handling, and deduplication.
- 195-byte MeshCore payload limit.
- Dry-run mode, history, transmit log, error log, and dashboard health status.

## Install

### Docker

```bash
docker compose up -d
```

Open `http://<host>:8000` and configure the BOM regions and MeshCore connection in Settings. The compose file mounts `/data` for the SQLite database and `/dev` for USB serial access.

### Native Linux

```bash
./install.sh
```

The installer creates the application environment and system service. Configure the service's MeshCore serial port or TCP host through the web UI.

### Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## BOM feed behavior

The application uses BOM's public state-based warning RSS feeds, including the warning link and product identifier supplied by BOM. RSS items are normalized into provider-neutral alerts before filtering and deduplication. BOM's feed documentation notes that RSS should not be the sole source of warning information and requires links back to the full BOM warning product.

## MeshCore setup

1. Open Settings.
2. Select the BOM state or territory feeds to monitor.
3. Optionally enter forecast district names.
4. Configure MeshCore as USB serial or TCP and select the live/test channels. For USB, click Auto-detect USB and select a companion radio from the detected-device dropdown; Linux selections prefer a stable `/dev/serial/by-id/` path. A serial path can still be entered manually when detection is unavailable. Click Save settings to persist the selected port.
5. Leave dry-run enabled while checking the dashboard and history.
6. Send a manual test before enabling live broadcasts.

WXEcho retries a saved but disconnected radio in the background about every 15 seconds. A stable by-id path survives `/dev/ttyUSB*` renumbering after a replug; without one, reconnecting requires the saved device path to remain the same.

### Companion settings

The MeshCore Settings page reads the saved radio's name, firmware, battery, radio parameters and configured channels from the connected companion. It can edit the device name and existing channel names without changing channel keys. PINs, keys, radio parameters and reset controls are not editable there.

Device changes are disabled by default. The example Compose file publishes port 8110 on all interfaces, which would let a visitor bypass an authenticated reverse proxy. Before enabling edits behind Authentik, restrict direct access to that port: bind it to `127.0.0.1:8110:8000` when the proxy runs on the same host, or restrict it with a private network/firewall when the proxy runs elsewhere. Only then set `WX_ECHO_DEVICE_WRITES_ENABLED=1` in the app environment and restart WXEcho. The server rejects edit requests when this flag is unset, even if a client sends a POST directly.

## License

MIT. See [LICENSE](LICENSE).
