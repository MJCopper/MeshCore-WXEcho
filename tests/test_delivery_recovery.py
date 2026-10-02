"""Pending notices survive restart and resume only unconfirmed parts."""
import pytest

from app.db import Database
from app.rfs.feed import Incident
from app.rfs.poller import RFSPoller


class Client:
    def __init__(self, incident):
        self.incident = incident

    async def fetch(self):
        return [self.incident]


class Tx:
    message_budget = 126
    queue_depth = 0

    def __init__(self):
        self.sent = []

    def enqueue_notice(self, parts, on_result=None, priority=3):
        self.sent.append((parts, on_result))
        return True

    def enqueue_verification(self, *args, **kwargs):
        return True


@pytest.mark.asyncio
async def test_rfs_partial_failure_and_restart_resume_only_missing_part(tmp_path):
    path = str(tmp_path / "delivery.db")
    incident = Incident("fire-1", "Long Bushland Fire Name", "Emergency Warning",
                        "Central Coast", "Long Bushland Road", "Not yet controlled; multiple roads affected; firefighters working in steep terrain",
                        "Bush Fire", "2026-10-01", "https://rfs.nsw.gov.au")
    db = Database(path)
    db.set_setting("rfs_enabled", True)
    db.set_setting("rfs_all_councils", True)
    db.set_setting("dry_run", False)
    tx = Tx()
    await RFSPoller(db, tx, Client(incident)).poll_once()
    parts, callback = tx.sent[0]
    assert len(parts) > 1
    callback(0, True, "")
    callback(1, False, "link down")
    for index in range(2, len(parts)):
        callback(index, True, "")
    row = db.rfs_latest_broadcast("fire-1")
    assert row["transmit_status"] == "failed"
    assert db.rfs_get_incident("fire-1")["last_sent_hash"] == ""
    db.close()

    db = Database(path)
    tx = Tx()
    await RFSPoller(db, tx, Client(incident)).poll_once()
    retried, callback = tx.sent[0]
    assert len(retried) == 1
    assert retried[0][0] == parts[1][0]
    callback(0, True, "")
    row = db.rfs_latest_broadcast("fire-1")
    assert row["transmit_status"] == "success"
    assert db.rfs_get_incident("fire-1")["last_sent_hash"] == incident.revision
    db.close()


def test_queued_history_becomes_interrupted_on_restart(tmp_path):
    path = str(tmp_path / "recovery.db")
    db = Database(path)
    row_id = db.add_service_history("bom", "warning-1", "Wind Warning",
                                    transmit_status="queued", transmitted_text="wind")
    db.close()
    db = Database(path)
    row = db.latest_history("warning-1")
    assert row["id"] == row_id
    assert row["transmit_status"] == "interrupted"
    db.close()


def test_dashboard_failure_query_shows_latest_unresolved_service_attempts():
    from datetime import datetime, timedelta, timezone

    db = Database(":memory:")
    since = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    failed_id = db.add_service_history("traffic", "road-1", "Road closure",
                                       transmit_status="failed", transmitted_text="closure")
    db.add_service_history("rfs", "fire-1", "Fire", transmit_status="failed")
    db.add_service_history("rfs", "fire-1", "Fire", transmit_status="queued")
    rows = db.recent_delivery_failures(since)
    assert [row["id"] for row in rows] == [failed_id]
    db.update_service_history(failed_id, "success")
    assert db.recent_delivery_failures(since) == []
    assert db.query_service_history(source="traffic")[0]["transmitted_at"]
    db.close()
