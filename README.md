# ⚠️ WARNING: UNOFFICIAL PERSONAL PROJECT — DO NOT RELY ON IT

> **This is an unofficial personal project. It is not suitable for serious, emergency, or safety-critical use. Do not trust any of its inputs or outputs.** Incoming data, settings, displayed warnings, and transmitted messages may be wrong, incomplete, delayed, or missing. Verify information independently using official Bureau of Meteorology and local emergency sources. Never make a safety decision based on WXEcho.

---

# MeshCore BOM Weather

MeshCore BOM Weather polls official Australian Bureau of Meteorology warning RSS feeds and broadcasts selected warnings over a MeshCore radio. It is a small self-hosted web app for a Raspberry Pi, Linux host, Windows machine, or Docker.

## Features

- State and territory BOM warning feeds.
- Optional forecast-district filtering.
- Configurable Australian IANA timezone display.
- MeshCore over USB serial or TCP.
- BOM warning filtering, update/cancellation handling, and deduplication.
- MeshCore channel messages are limited to 160 bytes including the companion name and `: `; WXEcho uses the connected name to size text and splits long alerts before sending.
- Dry-run mode, history, transmit log, error log, and dashboard health status.

## Install

### Docker

```bash
docker compose up -d
```

Open `http://<host>:8110` and configure the BOM regions and MeshCore connection in Settings. The compose file bind-mounts the repository's `data/` directory at `/data` for the SQLite database and mounts `/dev` for USB serial access.

### Data persistence and backup

Docker stores settings, BOM history, alert state, events and the transmit log in `data/wx-echo.db` on the host. `docker compose restart`, `stop`, `up` and container recreation retain this file. Keep the `data/` directory when moving or reinstalling WXEcho; a different checkout has a different `data/` directory.

After updating the application code, run `docker compose up -d --build` to rebuild and recreate the container. `docker compose restart` keeps the existing image and will not apply code changes.

For a consistent backup, stop the service before copying the database:

```bash
docker compose stop
cp data/wx-echo.db data/wx-echo.db.backup
docker compose start
```

The active database path and file size are shown on the Troubleshoot page. Native installations store the database in their installation's `data/` directory when run by the supplied systemd service.

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

Recent warning revisions and BOM History record each distinct version received from the selected feeds, including warnings excluded by broadcast filters. An unchanged warning is not added again on every poll. The Events log records each dry-run attempt, so it can grow while History stays the same. The unofficial verification message is queued after warning parts and sent at most once per five minutes on the live channel; its last successful send time survives restarts. Dry-run shows the same five-minute cadence.

The default broadcast policy includes all BOM warning products. Settings can instead select individual Australian warning products, plus additional products such as Flood Watch, Tropical Cyclone Advice, Road Weather Alert and Bush Walkers Weather Alert. Timestamped RSS titles and `Marine Wind Warning Summary` items are normalized before filtering; qualified flood products and numbered tropical cyclone products match their corresponding product selection. For marine wind warnings whose RSS item contains only a statewide summary, WXEcho resolves the linked BOM product ID and reads the warning detail API. Strong Wind Warning areas and cancellations are sent as separately labelled parts. If detail is unavailable, it falls back to the RSS summary.

## MeshCore setup

1. Open Settings.
2. Select the BOM state or territory feeds to monitor.
3. Optionally enter forecast district names.
4. Configure MeshCore as USB serial or TCP. For USB, choose a path from the always-visible device list; Auto-detect USB refreshes every entry in `/dev/serial/by-id/` without probing it. A serial path can also be entered manually. Live and test channel names load automatically from the chosen companion, and their selected indexes are retained when the radio is offline. Click Save settings to persist the port and channels.
5. Leave dry-run enabled while checking the dashboard and history.
6. Send a manual test before enabling live broadcasts.

WXEcho retries a saved but disconnected radio in the background about every 15 seconds. A stable by-id path survives `/dev/ttyUSB*` renumbering after a replug; without one, reconnecting requires the saved device path to remain the same.

### Companion settings

The MeshCore Settings page reads the saved radio's name, firmware, battery, TX power, radio parameters and configured channels from the connected companion. When the radio is connected, you can edit its name, TX power, frequency, bandwidth, spreading factor, coding rate and existing channel names. You can add a private channel in an empty slot with a supplied 16-byte key or a generated key, and remove an unused private channel. A generated key appears only on the creation response so it can be shared with other devices. The public slot and any slot selected as Live or Test cannot be removed. Channel renaming preserves the channel keys. Saved values are read back from the radio; model, firmware, battery and PINs are not editable on this page. The page is intended for a trusted network and does not have a separate device-write switch.

## License

MIT. See [LICENSE](LICENSE).
