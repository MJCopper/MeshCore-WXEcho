"""BOM current-feed snapshot is separate from broadcast history."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Database
from app.web.routes import router


def warning(region, alert_id, event="Flood Warning"):
    return {"region": region, "id": alert_id, "event": event,
            "headline": event + " for Hunter", "area_desc": "Hunter",
            "effective": "2026-10-01T00:00:00+00:00", "expires": "",
            "message_type": "Alert", "references": ["https://www.bom.gov.au/warning/test"]}


def test_successful_region_replaces_only_its_snapshot(tmp_path):
    path = tmp_path / "bom-current.db"
    db = Database(str(path))
    db.replace_bom_current([warning("NSW", "n1"), warning("VIC", "v1")],
                           {"NSW", "VIC"}, "2026-10-01T00:00:00+00:00")
    db.replace_bom_current([warning("NSW", "n2")], {"NSW"}, "2026-10-01T01:00:00+00:00")
    assert {row["alert_id"] for row in db.bom_current_items(["NSW", "VIC"])} == {"n2"}
    assert {row["region"]: row["fetched_at"] for row in db.bom_snapshot_regions(["NSW", "VIC"])} == {
        "NSW": "2026-10-01T01:00:00+00:00"}
    db.replace_bom_current([], {"NSW"}, "2026-10-01T02:00:00+00:00")
    assert db.bom_current_items(["NSW", "VIC"]) == []
    db.close()
    reopened = Database(str(path))
    assert reopened.bom_current_items(["VIC"]) == []
    reopened.close()


def test_bom_page_shows_current_snapshot_and_navigation():
    db = Database(":memory:")
    db.replace_bom_current([warning("NSW", "n1")], {"NSW"}, "2026-10-01T00:00:00+00:00")
    app = FastAPI()
    app.include_router(router)
    app.state.db = db
    app.state.tx = object()
    app.state.poller = SimpleNamespace(status=SimpleNamespace(last_poll_result="partial: VIC failed",
                                                               last_poll_time="2026-10-01T01:00:00+00:00"))
    page = TestClient(app).get("/bom")
    assert page.status_code == 200
    assert "Flood Warning" in page.text
    assert "Hunter" in page.text
    assert "Council match" in page.text
    assert "partial: VIC failed" in page.text
    assert 'href="/settings/bom"' in page.text
    assert 'href="/traffic"><svg' in page.text
    assert 'href="/bom"><svg' in page.text
    db.close()


async def _no_process(*args, **kwargs):
    return False


async def _exercise_partial_poller(monkeypatch):
    from app.poller import BomPoller

    class PartialClient:
        last_errors = ["VIC: unavailable"]
        last_successful_regions = {"NSW"}
        last_server_date = None

        async def fetch_active(self, regions, districts=None):
            return [warning("NSW", "n2")], "<rss/>"

    monkeypatch.setattr("app.poller.BOMClient", PartialClient)
    db = Database(":memory:")
    db.replace_bom_current([warning("NSW", "n1"), warning("VIC", "v1")],
                           {"NSW", "VIC"}, "2026-10-01T00:00:00+00:00")
    poller = BomPoller(db, object())
    monkeypatch.setattr(poller, "_process", _no_process)
    await poller.poll_once()
    assert {row["alert_id"] for row in db.bom_current_items(["NSW", "VIC"])} == {"n2"}
    assert poller.status.last_poll_result.startswith("partial:")
    db.close()


def test_partial_bom_poll_retains_failed_region(monkeypatch):
    import asyncio
    asyncio.run(_exercise_partial_poller(monkeypatch))


async def _exercise_disabled_poller(monkeypatch):
    from app.poller import BomPoller

    class CountingClient:
        calls = 0
        last_errors = []
        last_successful_regions = {"NSW"}
        last_server_date = None

        async def fetch_active(self, regions, districts=None):
            CountingClient.calls += 1
            return [warning("NSW", "n1")], "<rss/>"

    monkeypatch.setattr("app.poller.BOMClient", CountingClient)
    db = Database(":memory:")
    db.set_setting("bom_enabled", False)
    poller = BomPoller(db, object())
    monkeypatch.setattr(poller, "_process", _no_process)
    await poller.poll_once()
    assert CountingClient.calls == 0
    assert poller.status.last_poll_result == "disabled"
    assert not db.bom_current_items(["NSW"])
    db.set_setting("bom_enabled", True)
    await poller.poll_once()
    assert CountingClient.calls == 1
    assert db.bom_current_items(["NSW"])[0]["alert_id"] == "n1"
    db.close()


def test_bom_enable_switch_stops_and_resumes_fetches(monkeypatch):
    import asyncio
    asyncio.run(_exercise_disabled_poller(monkeypatch))


def test_disable_during_bom_fetch_discards_inflight_result(monkeypatch):
    import asyncio
    from app.poller import BomPoller

    db = Database(":memory:")

    class DisableDuringFetch:
        last_errors = []
        last_successful_regions = {"NSW"}
        last_server_date = None

        async def fetch_active(self, regions, districts=None):
            db.set_setting("bom_enabled", False)
            return [warning("NSW", "n1")], "<rss/>"

    monkeypatch.setattr("app.poller.BOMClient", DisableDuringFetch)
    poller = BomPoller(db, object())
    async def fail_process(*args, **kwargs):
        raise AssertionError("disabled BOM item was processed")
    monkeypatch.setattr(poller, "_process", fail_process)
    asyncio.run(poller.poll_once())
    assert poller.status.last_poll_result == "disabled"
    assert not db.bom_current_items(["NSW"])
    db.close()
