"""Shared history migration and future-source registration."""
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Database
from app.history import register_history_source
from app.web.routes import router


def test_legacy_bom_and_rfs_rows_migrate_once(tmp_path):
    path = tmp_path / "old.db"
    db = Database(str(path))
    with db._lock:
        db._conn.execute(
            "INSERT INTO history(ts, alert_id, event, area, disposition, transmit_status, transmitted_text, detail, revision_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-09-30T01:00:00+00:00", "bom-1", "Flood Warning", "Hunter", "sent",
             "success", "BOM message", "", "rev-1"),
        )
        db._conn.execute(
            "INSERT INTO rfs_history(ts, incident_id, name, level, council, status, transmitted_text, "
            "transmit_status, detail, revision_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-09-30T02:00:00+00:00", "rfs-1", "Hill Fire", "Watch and Act",
             "Central Coast", "Not yet controlled", "RFS message", "dry-run", "", "rev-2"),
        )
        db._conn.execute("DELETE FROM history_migrations")
        db._conn.commit()
    db.close()
    migrated = Database(str(path))
    rows = migrated.query_service_history()
    assert len(rows) == 2
    assert {row["source"] for row in rows} == {"bom", "rfs"}
    assert next(row for row in rows if row["source"] == "rfs")["metadata"]["level"] == "Watch and Act"
    migrated.close()
    reopened = Database(str(path))
    assert len(reopened.query_service_history()) == 2
    reopened.close()


def test_registered_future_service_uses_same_store_and_filters(tmp_path):
    register_history_source("sample", "Sample Service", "Severity", "severity", ("High", "Low"))
    db = Database(str(tmp_path / "future.db"))
    db.add_service_history("sample", "event-1", "Sample alert", "Region", "new",
                           "message", transmit_status="success", revision_hash="r1",
                           metadata={"severity": "High", "reference": "example"})
    assert db.query_service_history(source="sample", facet_key="severity", facet_value="High")[0]["title"] == "Sample alert"
    assert db.query_service_history(source="sample", facet_key="severity", facet_value="Low") == []
    assert db.query_service_history(source="sample", facet_key="unknown", facet_value="High") == []
    db.close()


def test_history_page_combines_bom_and_rfs_records():
    db = Database(":memory:")
    db.add_history("b1", "Flood Warning", "Hunter", "sent", "BOM message", transmit_status="success")
    db.add_service_history("rfs", "r1", "Hill Fire", "Central Coast", "",
                           "RFS message", transmit_status="dry-run",
                           metadata={"level": "Watch and Act", "status": "Not yet controlled"})
    app = FastAPI()
    app.include_router(router)
    app.state.db = db
    app.state.tx = object()
    page = TestClient(app).get("/history")
    assert page.status_code == 200
    assert "Flood Warning" in page.text and "Hill Fire" in page.text
    assert "BOM" in page.text and "NSW RFS" in page.text
    filtered = TestClient(app).get("/history?source=rfs&facet=Watch+and+Act")
    assert "Hill Fire" in filtered.text and "Flood Warning" not in filtered.text
    db.close()


def test_history_defaults_to_prepared_messages_and_can_show_all_records():
    db = Database(":memory:")
    db.add_history("bom-excluded", "Excluded BOM", "Hunter", "filtered", "")
    db.add_history("bom-queued", "Queued BOM", "Hunter", "sent", "BOM message",
                   transmit_status="queued")
    db.add_service_history("rfs", "rfs-failed", "Failed RFS", "Central Coast",
                           transmitted_text="RFS message", transmit_status="failed")
    db.add_service_history("traffic", "traffic-dry", "Dry Traffic", "Tamworth Regional",
                           transmitted_text="Traffic message", transmit_status="dry-run")
    db.add_service_history("traffic", "traffic-excluded", "Excluded Traffic", "Tamworth Regional",
                           disposition="excluded-council")
    app = FastAPI()
    app.include_router(router)
    app.state.db = db
    app.state.tx = object()
    client = TestClient(app)

    default = client.get("/history")
    assert default.status_code == 200
    assert 'value="prepared" selected' in default.text
    for title in ("Queued BOM", "Failed RFS", "Dry Traffic"):
        assert title in default.text
    for title in ("Excluded BOM", "Excluded Traffic"):
        assert title not in default.text

    partial = client.get("/partials/history")
    assert "Queued BOM" in partial.text
    assert "Excluded BOM" not in partial.text

    all_records = client.get("/history?records=all&source=traffic")
    assert 'value="all" selected' in all_records.text
    assert "Dry Traffic" in all_records.text
    assert "Excluded Traffic" in all_records.text
    assert "Queued BOM" not in all_records.text

    failed_only = client.get("/history?records=prepared&transmit_status=failed")
    assert "Failed RFS" in failed_only.text
    assert "Queued BOM" not in failed_only.text
    db.close()
