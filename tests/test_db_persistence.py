import sqlite3

from app.db import Database


def test_database_data_survives_restart(tmp_path):
    path = str(tmp_path / "wx-echo.db")
    db = Database(path)
    db.set_setting("bom_regions", ["NSW", "ACT"])
    db.upsert_state(
        alert_id="IDN21001",
        event="Severe Weather Warning",
        headline="Severe Weather Warning for Illawarra",
        expires="2026-09-30T12:00:00+00:00",
        msg_hash="abc123",
        disposition="sent",
        sent_ts="2026-09-29T10:00:00+00:00",
    )
    db.add_history(
        "IDN21001",
        "Severe Weather Warning",
        "Illawarra",
        "sent",
        "warning text",
        "new alert",
        transmit_status="success",
    )
    db.add_transmit_log(0, 12, True, "warning text", transport="meshcore")
    db.add_event("INFO", "queued warning")
    db.add_error("radio", "temporary failure")
    db.close()

    reopened = Database(path)

    assert reopened.get_setting("bom_regions") is None
    assert reopened.get_setting("bom_all_councils") is True
    assert reopened.get_state("IDN21001")["msg_hash"] == "abc123"
    assert reopened.query_history()[0]["transmit_status"] == "success"
    assert reopened.query_transmit_log()[0]["transport"] == "meshcore"
    assert reopened.recent_events()[0]["message"] == "queued warning"
    assert reopened.recent_errors()[0]["message"] == "temporary failure"
    reopened.close()


def test_history_end_date_includes_entire_day(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "history.db"))
    monkeypatch.setattr("app.db._now", lambda: "2026-07-23T23:59:59+00:00")
    db.add_history("late", "Flood Warning", "Hunter", "sent")

    rows = db.query_history(date_to="2026-07-23")

    assert [row["alert_id"] for row in rows] == ["late"]
    db.close()


def test_history_filters_by_meshcore_delivery_status(tmp_path):
    db = Database(str(tmp_path / "delivery.db"))
    db.add_history("ok", "Flood Warning", "Hunter", "sent", transmit_status="success")
    db.add_history("failed", "Flood Warning", "Hunter", "sent", transmit_status="failed")

    rows = db.query_history(transmit_status="failed")

    assert [row["alert_id"] for row in rows] == ["failed"]
    db.close()


def test_existing_database_gains_history_delivery_column(tmp_path):
    path = str(tmp_path / "legacy.db")
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            alert_id TEXT,
            event TEXT,
            area TEXT,
            disposition TEXT,
            transmitted_text TEXT,
            detail TEXT
        );
        CREATE TABLE transmit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            channel INTEGER,
            byte_count INTEGER,
            success INTEGER,
            manual INTEGER,
            text TEXT,
            error TEXT
        );
    """)
    connection.close()

    db = Database(path)
    history_columns = {
        row["name"] for row in db._conn.execute("PRAGMA table_info(history)")
    }
    transmit_columns = {
        row["name"] for row in db._conn.execute("PRAGMA table_info(transmit_log)")
    }

    assert "transmit_status" in history_columns
    assert "revision_hash" in history_columns
    assert "transport" in transmit_columns
    db.close()


def test_restart_removes_state_created_by_legacy_dry_run(tmp_path):
    path = str(tmp_path / "dry-run.db")
    db = Database(path)
    db.add_history(
        "dry-run-alert", "Flood Warning", "Hunter", "sent",
        transmit_status="dry-run",
    )
    db.upsert_state(
        alert_id="dry-run-alert",
        event="Flood Warning",
        headline="Flood Warning for Hunter",
        expires="2026-09-30T12:00:00+00:00",
        msg_hash="dry-run-hash",
        disposition="sent",
        sent_ts="2026-09-29T10:00:00+00:00",
    )
    db.close()

    migrated = Database(path)

    assert migrated.get_state("dry-run-alert") is None
    assert migrated.query_history()[0]["transmit_status"] == "dry-run"
    migrated.close()


def test_history_revisions_keep_independent_delivery_status(tmp_path):
    db = Database(str(tmp_path / "revisions.db"))
    first_id = db.add_history("same-link", "Flood Warning", "Hunter", "sent",
                              transmit_status="queued", revision_hash="old")
    second_id = db.add_history("same-link", "Flood Warning", "Hunter", "update",
                               transmit_status="queued", revision_hash="new")

    assert db.latest_history("same-link")["id"] == second_id
    assert db.latest_history("same-link")["revision_hash"] == "new"
    db.update_history_transmit_status(first_id, "failed")
    db.update_history_transmit_status(second_id, "success")

    rows = db.query_history()
    assert [(row["id"], row["transmit_status"]) for row in rows] == [
        (second_id, "success"), (first_id, "failed")]
    db.close()
