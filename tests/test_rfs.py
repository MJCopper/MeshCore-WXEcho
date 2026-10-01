"""NSW RFS feed, council selection, persistence and UI behavior."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Database
from app.rfs.feed import Incident, council_key, parse_incidents
from app.rfs.poller import RFSPoller, format_incident
from app.rfs.web import router


def incident(incident_id="1", council="Central Coast", level="Watch and Act", status="Not yet controlled"):
    return Incident(incident_id, "Bushland Fire", level, council, "Bushland Road", status,
                    "Bush Fire", "1 Oct 2026 14:00", "https://www.rfs.nsw.gov.au/fire-information/fires-near-me")


class FakeClient:
    def __init__(self, items):
        self.items = items

    async def fetch(self):
        return self.items


class FakeTx:
    message_budget = 145

    def __init__(self):
        self.sent = []
        self.verification = []

    def enqueue(self, text, on_result=None, delay_after=None):
        self.sent.append((text, on_result))
        return True

    def enqueue_verification(self, text, on_result=None, allow_new=True):
        self.verification.append((text, on_result, allow_new))
        return True


def test_parse_rfs_council_and_alert_level():
    payload = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {
        "guid": "incident-1", "title": "Bushland Fire", "category": "Emergency Warning",
        "description": "ALERT LEVEL: Emergency Warning <br />COUNCIL AREA: Central Coast <br />"
                       "STATUS: Not yet controlled <br />TYPE: Bush Fire <br />LOCATION: Bushland Road",
    }}]}
    item = parse_incidents(payload)[0]
    assert (item.incident_id, item.level, item.council, item.kind) == (
        "incident-1", "Emergency Warning", "Central Coast", "Bush Fire")
    assert council_key("Tamworth Regional") == council_key("Tamworth")
    assert council_key("Mid-Western Regional") == council_key("Mid-Western")


@pytest.mark.asyncio
async def test_rfs_filters_all_levels_by_selected_council_and_tracks_updates():
    db = Database(":memory:")
    db.set_setting("rfs_enabled", True)
    db.set_setting("rfs_councils", ["Central Coast"])
    db.set_setting("rfs_levels", ["Emergency Warning", "Watch and Act"])
    db.set_setting("dry_run", False)
    tx = FakeTx()
    client = FakeClient([incident("1", "Central Coast"), incident("2", "Cessnock", "Emergency Warning")])
    poller = RFSPoller(db, tx, client)
    await poller.poll_once()
    assert len(tx.sent) == 1
    assert "NSW RFS Watch and Act" in tx.sent[0][0]
    assert len(tx.verification) == 1
    tx.sent[0][1](True, "")
    assert db.rfs_get_incident("1")["last_sent_hash"] == incident().revision
    assert not db.rfs_get_incident("2")["last_sent_hash"]
    await poller.poll_once()
    assert len(tx.sent) == 1
    client.items = [incident("1", "Central Coast", status="Under control")]
    await poller.poll_once()
    assert len(tx.sent) == 2
    assert "Under control" in tx.sent[1][0]
    assert len(db.rfs_history()) == 3
    assert any(row["disposition"] == "excluded-council" for row in db.rfs_history())
    db.close()


@pytest.mark.asyncio
async def test_rfs_dry_run_previews_without_radio_send():
    db = Database(":memory:")
    db.set_setting("rfs_enabled", True)
    db.set_setting("rfs_all_councils", True)
    tx = FakeTx()
    poller = RFSPoller(db, tx, FakeClient([incident(level="Emergency Warning"),
                                                 incident("act", "ACT", "Emergency Warning")]))
    await poller.poll_once()
    assert poller.last_successful_poll == poller.last_poll
    assert tx.sent == []
    assert len(db.rfs_history()) == 2
    assert any(row["transmit_status"] == "dry-run" for row in db.rfs_history())
    assert any(row["disposition"] == "excluded-council" for row in db.rfs_history())
    db.close()


def test_rfs_page_offers_all_councils_and_saves_selection():
    db = Database(":memory:")
    app = FastAPI()
    app.include_router(router)
    app.state.db = db
    app.state.tx = FakeTx()
    app.state.rfs_poller = SimpleNamespace(last_result="disabled", last_poll="", poke=lambda: None)
    client = TestClient(app)
    response = client.get("/settings/rfs")
    assert response.status_code == 200
    assert "Central Coast" in response.text
    assert 'name="rfs_all_councils"' in response.text
    assert 'name="rfs_poll_minutes" type="number" min="5"' in response.text
    response = client.post("/rfs/settings", data={"rfs_enabled": "1", "rfs_councils": "Central Coast",
                                                  "rfs_levels": "Watch and Act"}, follow_redirects=False)
    assert response.status_code == 303
    assert db.get_setting("rfs_councils") == ["Central Coast"]
    assert db.get_setting("rfs_levels") == ["Watch and Act"]
    assert db.get_setting("rfs_poll_minutes") == 10
    client.post("/settings/rfs", data={"rfs_poll_minutes": "2"}, follow_redirects=False)
    assert db.get_setting("rfs_poll_minutes") == 5
    db.set_setting("rfs_councils", ["Central Coast"])
    db.set_setting("rfs_all_councils", True)
    page = client.get("/settings/rfs").text
    assert 'data-council-options class="council-choices is-disabled" aria-disabled="true"' in page
    assert 'name="rfs_councils" value="Central Coast" disabled checked' in page
    assert "updateCouncilChoices()" in page
    client.post("/settings/rfs", data={"rfs_enabled": "1", "rfs_all_councils": "1"},
                follow_redirects=False)
    assert db.get_setting("rfs_councils") == ["Central Coast"]
    db.close()


@pytest.mark.asyncio
async def test_new_council_queues_current_incident_and_recovers_interrupted_send():
    db = Database(":memory:")
    db.set_setting("rfs_enabled", True)
    db.set_setting("rfs_councils", ["Central Coast"])
    db.set_setting("rfs_levels", ["Watch and Act"])
    db.set_setting("dry_run", False)
    tx = FakeTx()
    client = FakeClient([incident("new", "Cessnock")])
    poller = RFSPoller(db, tx, client)
    await poller.poll_once()
    assert not tx.sent
    assert db.rfs_latest_history("new")["disposition"] == "excluded-council"
    db.set_setting("rfs_councils", ["Central Coast", "Cessnock"])
    await poller.poll_once()
    assert len(tx.sent) == 1
    assert db.rfs_latest_history("new")["transmit_status"] == "queued"
    assert db.rfs_recover_queued() == 1
    await poller.poll_once()
    assert len(tx.sent) == 2
    assert db.rfs_latest_history("new")["transmit_status"] == "queued"
    db.close()


@pytest.mark.asyncio
async def test_absence_requires_two_successful_polls_and_is_not_broadcast():
    db = Database(":memory:")
    db.set_setting("rfs_enabled", True)
    db.set_setting("rfs_all_councils", True)
    db.set_setting("rfs_levels", ["Watch and Act"])
    db.set_setting("dry_run", False)
    tx = FakeTx()
    client = FakeClient([incident()])
    poller = RFSPoller(db, tx, client)
    await poller.poll_once()
    tx.sent[0][1](True, "")
    client.items = []
    await poller.poll_once()
    assert len(db.rfs_history()) == 1
    await poller.poll_once()
    assert len(tx.sent) == 1
    assert db.rfs_latest_history("1")["disposition"] == "absent-from-feed"
    assert "resolution not confirmed" in db.rfs_latest_history("1")["detail"]
    db.close()


def test_rfs_source_note_matches_other_services():
    parts = format_incident(incident(), 145)
    assert parts[-1].endswith("; check rfs.nsw.gov.au")
    assert all(len(part.encode("utf-8")) <= 145 for part in parts)
