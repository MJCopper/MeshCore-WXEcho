"""SQLite storage: settings, alert state (dedupe), history, transmit log, errors.

The database is the source of truth. A single connection is shared across the
asyncio loop and the transmit worker thread, guarded by a lock. SQLite calls
are fast local operations, so running them synchronously is fine.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .config import DEFAULT_SETTINGS, STATE_EXPIRY_HOURS

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_state (
    alert_id    TEXT PRIMARY KEY,
    event       TEXT,
    headline    TEXT,
    expires     TEXT,
    msg_hash    TEXT,
    sent_ts     TEXT,
    disposition TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    alert_id         TEXT,
    event            TEXT,
    area             TEXT,
    disposition      TEXT,
    transmit_status  TEXT,
    transmitted_text TEXT,
    detail           TEXT,
    revision_hash    TEXT
);

CREATE TABLE IF NOT EXISTS bom_current (
    region TEXT NOT NULL,
    alert_id TEXT NOT NULL,
    event TEXT NOT NULL,
    headline TEXT NOT NULL,
    area TEXT NOT NULL,
    issued TEXT NOT NULL,
    expires TEXT NOT NULL,
    message_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY(region, alert_id)
);
CREATE TABLE IF NOT EXISTS bom_feed_snapshots (
    region TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '',
    disposition TEXT NOT NULL DEFAULT '',
    transmit_status TEXT,
    transmitted_text TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    revision_hash TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    legacy_id INTEGER,
    UNIQUE(source, legacy_id)
);
CREATE INDEX IF NOT EXISTS idx_service_history_ts ON service_history(ts, id);
CREATE INDEX IF NOT EXISTS idx_service_history_source ON service_history(source, ts);
CREATE INDEX IF NOT EXISTS idx_service_history_external ON service_history(source, external_id, id);
CREATE TABLE IF NOT EXISTS history_migrations (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS rfs_incidents (
    incident_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    level TEXT NOT NULL,
    council TEXT NOT NULL,
    location TEXT NOT NULL,
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    updated TEXT NOT NULL,
    source_url TEXT NOT NULL,
    revision_hash TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_sent_hash TEXT NOT NULL DEFAULT '',
    missing_polls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rfs_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    name TEXT NOT NULL,
    level TEXT NOT NULL,
    council TEXT NOT NULL,
    status TEXT NOT NULL,
    transmitted_text TEXT NOT NULL,
    transmit_status TEXT NOT NULL,
    detail TEXT NOT NULL,
    revision_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rfs_history_ts ON rfs_history(ts);

CREATE TABLE IF NOT EXISTS traffic_items (
    item_id TEXT PRIMARY KEY,
    feed TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    road TEXT NOT NULL,
    suburb TEXT NOT NULL,
    council TEXT NOT NULL,
    revision_hash TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_sent_hash TEXT NOT NULL DEFAULT '',
    missing_polls INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_traffic_last_seen ON traffic_items(last_seen);

CREATE TABLE IF NOT EXISTS transmit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    channel    INTEGER,
    byte_count INTEGER,
    success    INTEGER,
    manual     INTEGER,
    text       TEXT,
    error      TEXT,
    transport  TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    source TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT,
    message TEXT
);

CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts);
CREATE INDEX IF NOT EXISTS idx_history_disp ON history(disposition);
CREATE INDEX IF NOT EXISTS idx_history_alert_id ON history(alert_id, id);
CREATE INDEX IF NOT EXISTS idx_txlog_ts ON transmit_log(ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # A write that hits a lock should WAIT (up to 5s) for it to clear rather
        # than fail instantly with "database is locked".
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        self._init_schema()
        self._seed_settings()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            history_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(history)")
            }
            if "transmit_status" not in history_columns:
                self._conn.execute("ALTER TABLE history ADD COLUMN transmit_status TEXT")
            if "revision_hash" not in history_columns:
                self._conn.execute("ALTER TABLE history ADD COLUMN revision_hash TEXT")
            transmit_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(transmit_log)")
            }
            if "transport" not in transmit_columns:
                self._conn.execute("ALTER TABLE transmit_log ADD COLUMN transport TEXT")
            incident_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(rfs_incidents)")}
            if "missing_polls" not in incident_columns:
                self._conn.execute("ALTER TABLE rfs_incidents ADD COLUMN missing_polls INTEGER NOT NULL DEFAULT 0")
            self._conn.execute(
                "DELETE FROM alert_state WHERE alert_id IN "
                "(SELECT alert_id FROM history WHERE transmit_status = 'dry-run')"
            )
            self._migrate_service_history()
            self._conn.execute(
                "DELETE FROM alert_state WHERE alert_id IN "
                "(SELECT external_id FROM service_history WHERE source = 'bom' "
                "AND transmit_status = 'dry-run')"
            )
            self._conn.commit()

    def _migrate_service_history(self) -> None:
        """Copy old BOM/RFS rows once; keep old tables as untouched backup."""
        done = self._conn.execute(
            "SELECT 1 FROM history_migrations WHERE name = 'legacy_v1'"
        ).fetchone()
        if done:
            return
        self._conn.execute(
            """INSERT OR IGNORE INTO service_history
               (ts, source, external_id, title, area, disposition,
                transmit_status, transmitted_text, detail, revision_hash, metadata, legacy_id)
               SELECT ts, 'bom', COALESCE(alert_id, ''), COALESCE(event, ''),
                      COALESCE(area, ''), COALESCE(disposition, ''),
                      transmit_status, COALESCE(transmitted_text, ''),
                      COALESCE(detail, ''), COALESCE(revision_hash, ''), '{}', id
               FROM history"""
        )
        self._conn.execute(
            """INSERT OR IGNORE INTO service_history
               (ts, source, external_id, title, area, disposition,
                transmit_status, transmitted_text, detail, revision_hash, metadata, legacy_id)
               SELECT ts, 'rfs', incident_id, name, council, '',
                      transmit_status, transmitted_text, detail, revision_hash,
                      json_object('level', level, 'status', status), id
               FROM rfs_history"""
        )
        self._conn.execute(
            "INSERT INTO history_migrations(name) VALUES ('legacy_v1')"
        )

    def _seed_settings(self) -> None:
        with self._lock:
            cur = self._conn.execute("SELECT key FROM settings")
            existing = {r["key"] for r in cur.fetchall()}
            if "bom_poll_minutes" not in existing and "poll_interval" in existing:
                previous = self._conn.execute(
                    "SELECT value FROM settings WHERE key = 'poll_interval'"
                ).fetchone()
                try:
                    minutes = max(5, (int(json.loads(previous["value"])) + 59) // 60)
                except (TypeError, ValueError):
                    minutes = 5
                self._conn.execute(
                    "INSERT INTO settings(key, value) VALUES ('bom_poll_minutes', ?)",
                    (json.dumps(minutes),),
                )
                existing.add("bom_poll_minutes")
            for key, value in DEFAULT_SETTINGS.items():
                if key not in existing:
                    self._conn.execute(
                        "INSERT INTO settings(key, value) VALUES (?, ?)",
                        (key, json.dumps(value)),
                    )
            legacy_filter = self._conn.execute(
                "SELECT value FROM settings WHERE key = 'filter_include_exact'"
            ).fetchone()
            if legacy_filter and json.loads(legacy_filter["value"]) == ["Tornado Watch"]:
                self._conn.execute(
                    "UPDATE settings SET value = ? WHERE key = 'filter_include_exact'",
                    (json.dumps([]),),
                )
            # Old traffic code cached a multi-megabyte boundary map in settings.
            # The bundled map is now loaded once in memory and indexed there.
            self._conn.execute("DELETE FROM settings WHERE key = 'traffic_boundaries'")
            self._conn.commit()

    # ---- settings -------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def all_settings(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    # ---- current BOM feed snapshot ----------------------------------------
    def replace_bom_current(self, items: list[dict], successful_regions: set[str],
                            fetched_at: str) -> None:
        """Replace only regions fetched successfully; retain failed-region snapshots."""
        if not successful_regions:
            return
        with self._lock:
            for region in successful_regions:
                self._conn.execute("DELETE FROM bom_current WHERE region = ?", (region,))
                self._conn.execute(
                    "INSERT INTO bom_feed_snapshots(region, fetched_at) VALUES (?, ?) "
                    "ON CONFLICT(region) DO UPDATE SET fetched_at = excluded.fetched_at",
                    (region, fetched_at),
                )
            for item in items:
                region = item.get("region", "")
                if region not in successful_regions or not item.get("id"):
                    continue
                references = item.get("references") or []
                url = references[0] if references and str(references[0]).startswith(("https://", "http://")) else ""
                self._conn.execute(
                    """INSERT OR REPLACE INTO bom_current
                       (region, alert_id, event, headline, area, issued, expires,
                        message_type, source_url, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (region, item["id"], item.get("event", ""), item.get("headline", ""),
                     item.get("area_desc", ""), item.get("effective", ""),
                     item.get("expires", ""), item.get("message_type", "Alert"), url, fetched_at),
                )
            self._conn.commit()

    def bom_current_items(self, regions: list[str], limit: int = 500):
        if not regions:
            return []
        placeholders = ",".join("?" for _ in regions)
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM bom_current WHERE region IN ({placeholders}) "
                "ORDER BY region, issued DESC, headline LIMIT ?", (*regions, limit),
            ).fetchall()

    def bom_snapshot_regions(self, regions: list[str]):
        if not regions:
            return []
        placeholders = ",".join("?" for _ in regions)
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM bom_feed_snapshots WHERE region IN ({placeholders}) "
                "ORDER BY region", regions,
            ).fetchall()

    # ---- alert dedupe state --------------------------------------------
    def get_state(self, alert_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM alert_state WHERE alert_id = ?", (alert_id,)
            ).fetchone()

    def upsert_state(
        self,
        alert_id: str,
        event: str,
        headline: str,
        expires: str,
        msg_hash: str,
        disposition: str,
        sent_ts: Optional[str],
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO alert_state
                   (alert_id, event, headline, expires, msg_hash, sent_ts,
                    disposition, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(alert_id) DO UPDATE SET
                       event=excluded.event,
                       headline=excluded.headline,
                       expires=excluded.expires,
                       msg_hash=excluded.msg_hash,
                       sent_ts=COALESCE(excluded.sent_ts, alert_state.sent_ts),
                       disposition=excluded.disposition,
                       updated_at=excluded.updated_at""",
                (alert_id, event, headline, expires, msg_hash, sent_ts,
                 disposition, _now()),
            )
            self._conn.commit()

    def purge_expired_state(self) -> int:
        """Remove state rows 48h past their alert expiry."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=STATE_EXPIRY_HOURS)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM alert_state WHERE expires IS NOT NULL "
                "AND expires != '' AND expires < ?",
                (cutoff.isoformat(),),
            )
            self._conn.commit()
            return cur.rowcount

    # ---- shared service history -------------------------------------------
    def add_service_history(
        self, source: str, external_id: str, title: str, area: str = "",
        disposition: str = "", transmitted_text: str = "", detail: str = "",
        transmit_status: Optional[str] = None, revision_hash: str = "",
        metadata: Optional[dict] = None,
    ) -> int:
        from .history import get_history_source
        if get_history_source(source) is None:
            # Sources must register before writing; a typo must not fork history.
            raise ValueError(f"unregistered history source: {source}")
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO service_history
                   (ts, source, external_id, title, area, disposition,
                    transmit_status, transmitted_text, detail, revision_hash, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_now(), source, external_id, title, area, disposition,
                 transmit_status, transmitted_text, detail, revision_hash,
                 json.dumps(metadata or {})),
            )
            self._conn.commit()
            return cur.lastrowid

    def latest_service_history(self, source: str, external_id: str):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM service_history WHERE source = ? AND external_id = ? "
                "ORDER BY id DESC LIMIT 1", (source, external_id),
            ).fetchone()

    def update_service_history(self, row_id: int, transmit_status: str,
                               detail: Optional[str] = None) -> None:
        with self._lock:
            if detail is None:
                self._conn.execute(
                    "UPDATE service_history SET transmit_status = ? WHERE id = ?",
                    (transmit_status, row_id),
                )
            else:
                self._conn.execute(
                    "UPDATE service_history SET transmit_status = ?, detail = ? WHERE id = ?",
                    (transmit_status, detail, row_id),
                )
            self._conn.commit()

    def query_service_history(
        self, source: Optional[str] = None, disposition: Optional[str] = None,
        transmit_status: Optional[str] = None, date_from: Optional[str] = None,
        date_to: Optional[str] = None, limit: int = 200,
        facet_key: str = "", facet_value: str = "",
    ) -> list[dict]:
        clauses, params = [], []
        if facet_key and facet_value:
            from .history import get_history_source
            spec = get_history_source(source or "")
            if spec is None or spec.facet_key != facet_key or facet_value not in spec.facet_options:
                return []
            clauses.append("json_extract(metadata, ?) = ?")
            params.extend(("$." + facet_key, facet_value))
        if source:
            clauses.append("source = ?")
            params.append(source)
        if disposition:
            clauses.append("disposition = ?")
            params.append(disposition)
        if transmit_status:
            clauses.append("transmit_status = ?")
            params.append(transmit_status)
        if date_from:
            clauses.append("ts >= ?")
            params.append(date_from)
        if date_to:
            if "T" not in date_to:
                try:
                    next_day = datetime.fromisoformat(date_to) + timedelta(days=1)
                    clauses.append("ts < ?")
                    params.append(next_day.isoformat())
                except ValueError:
                    clauses.append("ts <= ?")
                    params.append(date_to)
            else:
                clauses.append("ts <= ?")
                params.append(date_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM service_history {where} ORDER BY ts DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item["metadata"] or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
            out.append(item)
        return out

    # Compatibility methods for the existing BOM poller and dashboard.
    def add_history(
        self, alert_id: str, event: str, area: str, disposition: str,
        transmitted_text: str = "", detail: str = "",
        transmit_status: Optional[str] = None, revision_hash: str = "",
    ) -> int:
        return self.add_service_history(
            "bom", alert_id, event, area, disposition, transmitted_text,
            detail, transmit_status, revision_hash,
        )

    def latest_history(self, alert_id: str):
        return self.latest_service_history("bom", alert_id)

    def refresh_dry_run_history_text(self, history_id: int, transmitted_text: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE service_history SET transmitted_text = ? WHERE id = ? "
                "AND source = 'bom' AND transmit_status = 'dry-run'",
                (transmitted_text, history_id),
            )
            self._conn.commit()

    def update_history_transmit_status(
        self, history_id: int, transmit_status: str, detail: Optional[str] = None,
    ) -> None:
        self.update_service_history(history_id, transmit_status, detail)

    def prune_history(self, keep_days: int = 90) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute("DELETE FROM service_history WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def query_history(
        self, disposition: Optional[str] = None, transmit_status: Optional[str] = None,
        date_from: Optional[str] = None, date_to: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        rows = self.query_service_history(
            source="bom", disposition=disposition, transmit_status=transmit_status,
            date_from=date_from, date_to=date_to, limit=limit,
        )
        for row in rows:
            row["alert_id"] = row["external_id"]
            row["event"] = row["title"]
        return rows

    # ---- Live Traffic NSW --------------------------------------------------
    def traffic_get_item(self, item_id: str):
        with self._lock:
            return self._conn.execute("SELECT * FROM traffic_items WHERE item_id = ?", (item_id,)).fetchone()

    def traffic_save_item(self, item, council: str, active: bool) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO traffic_items
                   (item_id, feed, title, category, road, suburb, council, revision_hash, last_seen, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET
                     feed=excluded.feed, title=excluded.title, category=excluded.category,
                     road=excluded.road, suburb=excluded.suburb, council=excluded.council,
                     revision_hash=excluded.revision_hash, last_seen=excluded.last_seen,
                     active=excluded.active, missing_polls=0""",
                (item.item_id, item.feed, item.title, item.category, item.road, item.suburb,
                 council, item.revision, _now(), int(active)),
            )
            self._conn.commit()

    def traffic_mark_sent(self, item_id: str, revision: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE traffic_items SET last_sent_hash = ? WHERE item_id = ?", (revision, item_id))
            self._conn.commit()

    def traffic_latest_broadcast(self, item_id: str):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM service_history WHERE source = 'traffic' AND external_id = ? "
                "AND transmit_status IS NOT NULL ORDER BY id DESC LIMIT 1", (item_id,),
            ).fetchone()

    def traffic_recover_queued(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE service_history SET transmit_status = 'interrupted', "
                "detail = 'Send interrupted before confirmation; current incident will be retried' "
                "WHERE source = 'traffic' AND transmit_status = 'queued'"
            )
            self._conn.commit()
            return cur.rowcount

    def traffic_missing_after_poll(self, seen_ids: set[str]) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM traffic_items").fetchall()
            missing = [row for row in rows if row["item_id"] not in seen_ids]
            for row in missing:
                self._conn.execute(
                    "UPDATE traffic_items SET missing_polls = missing_polls + 1, active = 0 WHERE item_id = ?",
                    (row["item_id"],),
                )
            self._conn.commit()
            return [dict(row) | {"missing_polls": row["missing_polls"] + 1} for row in missing]

    def traffic_recent_items(self, since: str, limit: int = 500):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM traffic_items WHERE last_seen >= ? "
                "ORDER BY active DESC, last_seen DESC, item_id LIMIT ?", (since, limit),
            ).fetchall()

    # ---- NSW RFS incidents and history ------------------------------------
    def rfs_get_incident(self, incident_id: str):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM rfs_incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()

    def rfs_save_incident(self, incident, revision_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO rfs_incidents
                   (incident_id, name, level, council, location, status, kind,
                    updated, source_url, revision_hash, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(incident_id) DO UPDATE SET
                     name=excluded.name, level=excluded.level, council=excluded.council,
                     location=excluded.location, status=excluded.status, kind=excluded.kind,
                     updated=excluded.updated, source_url=excluded.source_url,
                     revision_hash=excluded.revision_hash, last_seen=excluded.last_seen,
                    missing_polls=0""",
                (incident.incident_id, incident.name, incident.level, incident.council,
                 incident.location, incident.status, incident.kind, incident.updated,
                 incident.source_url, revision_hash, _now()),
            )
            self._conn.commit()

    def rfs_mark_sent(self, incident_id: str, revision_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE rfs_incidents SET last_sent_hash = ? WHERE incident_id = ?",
                (revision_hash, incident_id),
            )
            self._conn.commit()

    def rfs_add_history(self, incident, text: str, status: str, detail: str = "",
                        disposition: str = "") -> int:
        return self.add_service_history(
            "rfs", incident.incident_id, incident.name, incident.council,
            transmitted_text=text, detail=detail, transmit_status=status or None,
            disposition=disposition, revision_hash=incident.revision,
            metadata={"level": incident.level, "status": incident.status,
                      "kind": incident.kind, "location": incident.location,
                      "source_url": incident.source_url},
        )

    def rfs_latest_history(self, incident_id: str):
        return self.latest_service_history("rfs", incident_id)

    def rfs_latest_broadcast(self, incident_id: str):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM service_history WHERE source = 'rfs' AND external_id = ? "
                "AND transmit_status IS NOT NULL ORDER BY id DESC LIMIT 1", (incident_id,),
            ).fetchone()

    def rfs_recover_queued(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE service_history SET transmit_status = 'interrupted', "
                "detail = 'Send interrupted before confirmation; current incident will be retried' "
                "WHERE source = 'rfs' AND transmit_status = 'queued'"
            )
            self._conn.commit()
            return cur.rowcount

    def rfs_missing_after_poll(self, seen_ids: set[str]) -> list[dict]:
        """Advance disappearance counters only after a successful feed fetch."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM rfs_incidents").fetchall()
            missing = [row for row in rows if row["incident_id"] not in seen_ids]
            for row in missing:
                self._conn.execute(
                    "UPDATE rfs_incidents SET missing_polls = missing_polls + 1 WHERE incident_id = ?",
                    (row["incident_id"],),
                )
            self._conn.commit()
            return [dict(row) | {"missing_polls": row["missing_polls"] + 1} for row in missing]

    def rfs_update_history(self, row_id: int, status: str, detail: str = "") -> None:
        self.update_service_history(row_id, status, detail)

    def rfs_active_incidents(self, since: str, limit: int = 500):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM rfs_incidents WHERE last_seen >= ? "
                "ORDER BY CASE level WHEN 'Emergency Warning' THEN 0 "
                "WHEN 'Watch and Act' THEN 1 ELSE 2 END, name LIMIT ?",
                (since, limit),
            ).fetchall()

    def rfs_history(self, limit: int = 100):
        rows = self.query_service_history(source="rfs", limit=limit)
        for row in rows:
            row["incident_id"] = row["external_id"]
            row["name"] = row["title"]
            row["council"] = row["area"]
            row["level"] = row["metadata"].get("level", "")
            row["status"] = row["metadata"].get("status", "")
        return rows

    # ---- transmit log ---------------------------------------------------
    def add_transmit_log(
        self,
        channel: int,
        byte_count: int,
        success: bool,
        text: str,
        manual: bool = False,
        error: str = "",
        transport: str = "meshcore",
    ) -> None:
        cols = ("ts, channel, byte_count, success, manual, text, error, transport")
        vals = (_now(), channel, byte_count, int(success), int(manual), text, error, transport)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO transmit_log(%s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)" % cols, vals)
            except sqlite3.OperationalError:
                # migrate an older DB that predates the transport column
                self._conn.execute("ALTER TABLE transmit_log ADD COLUMN transport TEXT")
                self._conn.execute(
                    "INSERT INTO transmit_log(%s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)" % cols, vals)
            self._conn.commit()

    def query_transmit_log(self, limit: int = 200) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM transmit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def get_transmit_log(self, entry_id: int):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM transmit_log WHERE id = ?", (entry_id,)
            ).fetchone()

    # ---- errors ---------------------------------------------------------
    def add_error(self, source: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO errors(ts, source, message) VALUES (?, ?, ?)",
                (_now(), source, message),
            )
            self._conn.commit()

    def recent_errors(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def clear_errors(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM errors")
            self._conn.commit()
            return cur.rowcount

    # ---- events (dashboard feed) ---------------------------------------
    def add_event(self, level: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts, level, message) VALUES (?, ?, ?)",
                (_now(), level, message),
            )
            self._conn.commit()

    def recent_events(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(RESTART)")
            finally:
                self._conn.close()
