# ⚠️ WARNING: UNOFFICIAL PERSONAL PROJECT — DO NOT RELY ON IT

> **This is an unofficial personal project. It is not suitable for serious, emergency, or safety-critical use. Do not trust any of its inputs or outputs.** Incoming data, settings, displayed warnings, and transmitted messages may be wrong, incomplete, delayed, or missing. Verify information independently using official Bureau of Meteorology and local emergency sources. Never make a safety decision based on WXEcho.

---

# MeshCore WXEcho

WXEcho polls the NSW Bureau of Meteorology warning RSS feed, NSW RFS incident data and public Live Traffic NSW GeoJSON feeds, then broadcasts selected notices over a MeshCore radio. It is a small self-hosted web app for a Raspberry Pi, Linux host, Windows machine, or Docker.

## BOM current warnings

BOM, RFS and Live Traffic NSW each have their own enable control and polling interval in minutes (minimum 5). BOM monitors only the NSW warning feed. The **BOM** page shows the latest NSW feed snapshot, its last successful fetch time, and each warning's council match and selection result; broadcast History remains separate. BOM settings can select all NSW councils or individual councils. When a warning cannot be mapped reliably to councils, it is included by default and marked as unknown in History; this fallback can be disabled in settings.

## NSW RFS Fires Near Me

WXEcho can monitor the official NSW RFS current-incidents GeoJSON feed as a separate source. Open **NSW RFS** to enable monitoring, choose one or more NSW council areas (or All NSW), and select alert levels. Emergency Warning and Watch and Act are selected initially; Advice is optional. The council filter applies to every level. No RFS messages are sent until monitoring and council coverage are selected. The global Dry Run setting also applies to RFS.

RFS incidents have their own live view; BOM and RFS broadcast history appear together in History with a source filter. WXEcho checks the RFS feed at its configured interval (minimum 5 minutes); the RFS says incident data is updated every 30 minutes. Incident locations may be approximate. Source: © State of New South Wales (NSW Rural Fire Service). For current information go to [rfs.nsw.gov.au](https://www.rfs.nsw.gov.au/).

## Live Traffic NSW

Live Traffic NSW is a separate, disabled-by-default service. Configure it under **Settings → Live Traffic NSW** with council areas and hazard feeds. It uses keyless Transport for NSW public GeoJSON feeds and the global Dry Run setting. Incident, flood and local council feeds are selected initially; scheduled roadworks require an explicit Roadwork selection. Ended and future items are recorded but never broadcast. Fire-feed notices are suppressed while RFS monitoring is enabled to avoid duplicate fire broadcasts. Existing items on the first live poll are recorded as a baseline without transmitting; newly selected councils can then send currently active matching items. Feed disappearance is recorded after two successful polls without claiming a road has reopened.

State-road coordinates are matched locally against a simplified NSW Spatial Services council-boundary snapshot (`app/traffic/nsw_lga.geojson.gz`, obtained 1 October 2026); regional council names are a fallback. Boundary locations are approximate, especially near borders. Source: © Transport for NSW, [Live Traffic Hazards dataset](https://data.nsw.gov.au/data/dataset/2-live-traffic-hazards). Verify current conditions at [livetraffic.com](https://www.livetraffic.com/).

## Settings and history

The Settings hub has separate pages for General, BOM, NSW RFS, Live Traffic NSW, MeshCore connection and MeshCore companion controls. Saving one section does not replace settings in another. The History page combines BOM, RFS and Live Traffic NSW records by timestamp and can filter by source, delivery status and date. Source-specific details appear when available. Existing records are copied once into the shared history table on upgrade; the old tables remain untouched as a backup.

Additional services register a stable source ID and label in `app/history.py`, then write through `Database.add_service_history`. Optional source-specific metadata and a registered facet can be shown without adding another history table. The dashboard shows the enabled state, poll interval, and last successful poll for BOM, RFS and Live Traffic. Recent Notices contains only successfully transmitted service notices; Dry Run, queued, failed and excluded entries remain in History.

## Features

- NSW BOM warning feed with optional NSW council filtering.
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

Open `http://<host>:8110` and configure BOM warning products, council coverage and MeshCore connection in Settings. The compose file bind-mounts the repository's `data/` directory at `/data` for the SQLite database and mounts `/dev` for USB serial access.

### Data persistence and backup

Docker stores settings, shared service history, alert state, events and the transmit log in `data/wx-echo.db` on the host. `docker compose restart`, `stop`, `up` and container recreation retain this file. Keep the `data/` directory when moving or reinstalling WXEcho; a different checkout has a different `data/` directory.

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

The application uses BOM's public NSW warning RSS feed, including the warning link and product identifier supplied by BOM. RSS items are normalized into provider-neutral alerts before filtering and deduplication. BOM's feed documentation notes that RSS should not be the sole source of warning information and requires links back to the full BOM warning product.

Recent warning revisions and the History page record each distinct BOM version received from the NSW feed, including warnings excluded by broadcast filters. An unchanged warning is not added again on every poll. The Events log records each dry-run attempt, so it can grow while History stays the same. The unofficial verification message is queued after warning parts and sent at most once per five minutes on the live channel; its last successful send time survives restarts. Dry-run shows the same five-minute cadence.

The default broadcast policy includes all BOM warning products. Settings can instead select individual Australian warning products, plus additional products such as Flood Watch, Tropical Cyclone Advice, Road Weather Alert and Bush Walkers Weather Alert. Timestamped RSS titles and `Marine Wind Warning Summary` items are normalized before filtering; qualified flood products and numbered tropical cyclone products match their corresponding product selection. Council matching uses BOM warning polygons when supplied, or a complete list of council names. Broad forecast districts, marine coasts, and incomplete location descriptions are marked unknown rather than treated as outside a selected council. Existing non-NSW current snapshots and rows with proven non-NSW provenance are removed on upgrade; older History without reliable source provenance is retained. For marine wind warnings whose RSS item contains only a statewide summary, WXEcho resolves the linked BOM product ID and reads the warning detail API. Strong Wind Warning areas and cancellations are sent as separately labelled parts. If detail is unavailable, it falls back to the RSS summary.

## MeshCore setup

1. Open Settings.
2. Choose all NSW council areas or individual councils for BOM warnings.
3. Optionally enter forecast district names.
4. Configure MeshCore as USB serial or TCP. For USB, choose a path from the always-visible device list; Auto-detect USB refreshes every entry in `/dev/serial/by-id/` without probing it. A serial path can also be entered manually. Live and test channel names load automatically from the chosen companion, and their selected indexes are retained when the radio is offline. Click Save settings to persist the port and channels.
5. Leave dry-run enabled while checking the dashboard and history.
6. Send a manual test before enabling live broadcasts.

WXEcho retries a saved but disconnected radio in the background about every 15 seconds. A stable by-id path survives `/dev/ttyUSB*` renumbering after a replug; without one, reconnecting requires the saved device path to remain the same.

### Companion settings

The MeshCore Settings page reads the saved radio's name, firmware, battery, TX power, radio parameters and configured channels from the connected companion. When the radio is connected, you can edit its name, TX power, frequency, bandwidth, spreading factor, coding rate and existing channel names. You can add a private channel in an empty slot with a supplied 16-byte key or a generated key, and remove an unused private channel. A generated key appears only on the creation response so it can be shared with other devices. The public slot and any slot selected as Live or Test cannot be removed. Channel renaming preserves the channel keys. Saved values are read back from the radio; model, firmware, battery and PINs are not editable on this page. The page is intended for a trusted network and does not have a separate device-write switch.

## License

MIT. See [LICENSE](LICENSE).
