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
            self._conn.execute(
                "DELETE FROM alert_state WHERE alert_id IN "
                "(SELECT alert_id FROM history WHERE transmit_status = 'dry-run')"
            )
            self._conn.commit()

    def _seed_settings(self) -> None:
        with self._lock:
            cur = self._conn.execute("SELECT key FROM settings")
            existing = {r["key"] for r in cur.fetchall()}
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

    # ---- history --------------------------------------------------------
    def add_history(
        self,
        alert_id: str,
        event: str,
        area: str,
        disposition: str,
        transmitted_text: str = "",
        detail: str = "",
        transmit_status: Optional[str] = None,
        revision_hash: str = "",
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO history(ts, alert_id, event, area, disposition, "
                "transmit_status, transmitted_text, detail, revision_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_now(), alert_id, event, area, disposition,
                 transmit_status, transmitted_text, detail, revision_hash),
            )
            self._conn.commit()
            return cur.lastrowid

    def latest_history(self, alert_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT id, revision_hash FROM history WHERE alert_id = ? "
                "ORDER BY id DESC LIMIT 1", (alert_id,),
            ).fetchone()

    def refresh_dry_run_history_text(self, history_id: int, transmitted_text: str) -> None:
        """Refresh a prepared preview while keeping its original revision row."""
        with self._lock:
            self._conn.execute(
                "UPDATE history SET transmitted_text = ? WHERE id = ? "
                "AND transmit_status = 'dry-run'",
                (transmitted_text, history_id),
            )
            self._conn.commit()

    def update_history_transmit_status(
        self, history_id: int, transmit_status: str, detail: Optional[str] = None,
    ) -> None:
        with self._lock:
            if detail is None:
                self._conn.execute(
                    "UPDATE history SET transmit_status = ? WHERE id = ?",
                    (transmit_status, history_id),
                )
            else:
                self._conn.execute(
                    "UPDATE history SET transmit_status = ?, detail = ? WHERE id = ?",
                    (transmit_status, detail, history_id),
                )
            self._conn.commit()

    def prune_history(self, keep_days: int = 90) -> int:
        # Delete history rows older than keep_days; bounds long-term growth.
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=keep_days)
        ).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM history WHERE ts < ?", (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount

    def query_history(
        self,
        disposition: Optional[str] = None,
        transmit_status: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
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
            return self._conn.execute(
                f"SELECT * FROM history {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()

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
