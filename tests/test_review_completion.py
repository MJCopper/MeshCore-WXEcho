"""End-to-end regressions for the six follow-up review changes."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
import respx

from app.bom_area import match_councils
from app.bom_enricher import parse_warning_api
from app.config import QUEUE_MAX, QUEUE_BYTE_MAX
from app.db import Database
from app.delivery import queue_refusal, permanently_unsendable
from app.filters import FilterRules
from app.poller import BomPoller
from app.presentation import freshness
from app.rfs.feed import Incident, parse_incidents
from app.rfs.poller import format_incident
from app.rfs.web import router as rfs_router
from app.traffic.feed import BASE_URL, FEEDS, TrafficClient, TrafficFeedError, TrafficItem, match_traffic_council, parse_feed, prepare_councils
from app.traffic.poller import TrafficPoller, format_item
from app.traffic.schedule import ClosurePeriod, window_state
from app.traffic.web import router as traffic_router
from app.transmit import TransmitManager
from app.web.routes import router as bom_router


def item(identifier="incident:1", **changes):
    return replace(TrafficItem(identifier, "incident", "CRASH", "CRASH", "Highway", "Town", "Tamworth Regional Council",
                               "Northbound", "Lane closed", "Use bypass", None, None, None, None, False, None), **changes)


@pytest.mark.parametrize("area", ["Sydney", "Tamworth", "Central Coast", "Sydney and North Sydney", "Hunter", "Sydney Council and Hunter"])
def test_untyped_places_cannot_prove_a_council_footprint(area):
    match = match_councils(area, (), ())
    assert match.status == "unknown" and match.reason


def test_typed_lga_provenance_survives_normalisation_but_not_mixed_districts():
    raw = {"warning": {"info": [{"area": [{"geocode": [
        {"name": "New South Wales", "type": "aac:region"},
        {"name": "Sydney", "type": "aac:lga"}]}]}]}}
    e = parse_warning_api(raw)
    match = match_councils("Sydney", e.area_names, e.polygons, typed_lgas=e.lga_names)
    assert match.councils == ("Sydney",) and match.method == "typed LGA names"
    raw["warning"]["info"][0]["area"][0]["geocode"].append({"name": "Hunter", "type": "aac:district"})
    assert not parse_warning_api(raw).lga_names


def test_incomplete_polygon_cannot_fall_back_to_a_confident_name_match():
    polygons = prepare_councils([{"properties": {"lganame": "Sydney"}, "geometry": {
        "type": "Polygon", "coordinates": [[[150,-34],[152,-34],[152,-32],[150,-32]]]}}])
    result = match_councils("Sydney Council", (), ("-33,151 -33,151.5 -32.5,151", "broken"), polygons)
    assert result.status == "unknown"


class TestRadio:
    __test__ = False
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
async def test_long_notice_streams_all_parts_in_order_and_confirms_each_index(monkeypatch):
    db = Database(":memory:")
    tx = TransmitManager(db)
    parts = [(f"part-{index}", 0) for index in range(41)]
    outcomes, texts = [], []
    assert tx.enqueue_notice(parts, lambda index, ok, error: outcomes.append((index, ok)))
    assert len(tx._queue) == 1 and tx.queue_depth == 41
    async def send(part):
        texts.append(part.text)
        if len(texts) == 41:
            tx._stopped = True
        return True, ""
    monkeypatch.setattr(tx, "_transmit_item", send)
    await asyncio.wait_for(tx._worker(), 2)
    assert texts == [text for text, _ in parts]
    assert outcomes == [(index, True) for index in range(41)]
    db.close()


def test_notice_and_byte_limits_defer_atomically_without_permanent_failure():
    db = Database(":memory:")
    tx = TransmitManager(db)
    for index in range(QUEUE_MAX):
        assert tx.enqueue_notice([("short", 0)] * 41)
    before = tx.queue_depth
    assert not tx.enqueue_notice([("new", 0)] * 41)
    assert tx.queue_depth == before
    tx = TransmitManager(db)
    assert not tx.enqueue_notice([("a" * (QUEUE_BYTE_MAX + 1), 0)])
    assert not tx._queue
    assert queue_refusal(["part"] * 41)[0] == "deferred"
    assert not permanently_unsendable({"detail": "Notice has 41 parts; queue limit is 20"}, "same", "same")
    db.close()


@pytest.mark.asyncio
async def test_long_notice_restart_resumes_only_unconfirmed_parts(tmp_path, monkeypatch):
    monkeypatch.setattr("app.traffic.poller.format_item", lambda *a, **k: [f"Delivery test part {i}" for i in range(25)])
    path = str(tmp_path / "restart.db")
    db = Database(path)
    db.set_setting("traffic_enabled", True)
    db.set_setting("traffic_all_councils", True)
    db.set_setting("traffic_baseline_done", True)
    db.set_setting("dry_run", False)
    event = item(advice="; ".join(f"Detailed provider advisory number {i}" for i in range(80)))
    class Client:
        async def fetch(self):
            return [event]
        async def boundaries(self):
            return []
    radio = TestRadio()
    await TrafficPoller(db, radio, Client()).poll_once()
    parts, callback = radio.sent[0]
    assert len(parts) > 20
    for index in range(5):
        callback(index, True, "")
    db.close()
    db = Database(path)
    radio = TestRadio()
    await TrafficPoller(db, radio, Client()).poll_once()
    resumed, callback = radio.sent[0]
    assert resumed == parts[5:]
    for index in range(len(resumed)):
        callback(index, True, "")
    assert db.latest_service_history("traffic", event.item_id)["transmit_status"] == "success"
    db.close()


def epoch(iso):
    return datetime.fromisoformat(iso).timestamp()


@pytest.mark.parametrize("stamp,expected", [
    ("2026-10-02T23:00:00+10:00", "scheduled"),
    ("2026-10-03T01:00:00+10:00", "scheduled"),
    ("2026-10-03T07:00:00+10:00", "unscheduled"),
    ("2026-10-04T01:00:00+10:00", "unscheduled"),
])
def test_overnight_weekday_window_carries_to_the_following_day(stamp, expected):
    periods = (ClosurePeriod("Weekdays", "", "10:00pm", "5:00am", "Australia/Sydney"),)
    assert window_state(periods, epoch(stamp))[0] == expected


@pytest.mark.parametrize("stamp,expected", [
    ("2026-10-04T01:45:00+10:00", "scheduled"),
    ("2026-10-04T03:15:00+11:00", "scheduled"),
    ("2026-10-04T03:30:00+11:00", "unscheduled"),
    ("2026-04-05T02:15:00+11:00", "scheduled"),
    ("2026-04-05T02:15:00+10:00", "scheduled"),
])
def test_schedule_uses_provider_timezone_across_dst(stamp, expected):
    periods = (ClosurePeriod("Sunday", "", "1:30am", "3:30am", "Australia/Sydney"),)
    assert window_state(periods, epoch(stamp))[0] == expected


@pytest.mark.parametrize("period", [
    ClosurePeriod("Weekdays", "", "7:00am", "5:00pm"),
    ClosurePeriod("Monday", "Friday", "7:00am", "5:00pm", "Australia/Sydney"),
    ClosurePeriod("Every Day", "", "invalid", "5:00pm", "Australia/Sydney"),
])
def test_unestablished_schedule_semantics_remain_unknown(period):
    assert window_state((period,), time.time())[0] == "unknown"


def test_scheduled_notice_preserves_schedule_and_does_not_claim_current_closure():
    event = item(feed="roadwork", category="SCHEDULED ROADWORK", periods=(ClosurePeriod("Weekdays", "", "7:00am", "5:00pm"),),
                 public_transport="Buses diverted", additional_info="Keep emergency access clear")
    parts = format_item(event, "Tamworth Regional", 126)
    text = " || ".join(parts)
    for fact in ("Scheduled", "Weekdays", "7:00am", "5:00pm", "TZ unknown",
                 "closure unconfirmed"):
        assert fact in text
    assert all(len(part.encode()) <= 126 for part in parts)
    assert len(parts) <= 2
    assert event.active()
    assert event.closure_window()[0] == "unknown"


def payload(identifier=1):
    return {"type": "FeatureCollection", "lastPublished": int(time.time()*1000), "features": [
        {"id": identifier, "properties": {"mainCategory": "CRASH", "roads": []}, "geometry": None}]}


@pytest.mark.asyncio
async def test_partial_fetch_keeps_healthy_items_and_avoids_unselected_requests():
    with respx.mock(assert_all_called=False) as router:
        healthy = router.get(BASE_URL + FEEDS["incident"]).respond(200, json=payload())
        router.get(BASE_URL + FEEDS["flood"]).respond(503)
        unselected = router.get(BASE_URL + FEEDS["roadwork"]).respond(503)
        client = TrafficClient()
        events = await client.fetch({"incident", "flood"})
    assert len(events) == 1 and healthy.called and not unselected.called
    assert client.last_successful_feeds == {"incident"}
    assert set(client.last_errors) == {"flood"}
    assert client.last_published["incident"]


@pytest.mark.asyncio
async def test_all_failed_fetch_reports_each_feed():
    with respx.mock() as router:
        for feed in ("incident", "flood"):
            router.get(BASE_URL + FEEDS[feed]).respond(503)
        client = TrafficClient()
        with pytest.raises(TrafficFeedError):
            await client.fetch({"incident", "flood"})
    assert not client.last_successful_feeds and set(client.last_errors) == {"incident", "flood"}


@pytest.mark.asyncio
async def test_partial_poll_does_not_advance_failed_feed_absence_or_repeat_healthy_baseline():
    db = Database(":memory:")
    db.set_setting("traffic_enabled", True)
    db.set_setting("traffic_all_councils", True)
    db.set_setting("dry_run", False)
    db.traffic_save_item(item("flood:old", feed="flood"), "Tamworth Regional", True)
    class Client:
        last_successful_feeds = {"incident"}
        last_errors = {"flood": "offline"}
        last_published = {"incident": time.time()}
        events = [item()]
        async def fetch(self):
            return self.events
        async def boundaries(self):
            return []
    client = Client()
    radio = TestRadio()
    poller = TrafficPoller(db, radio, client)
    await poller.poll_once()
    assert not radio.sent and "partial" in poller.last_result
    assert db.traffic_get_item("flood:old")["missing_polls"] == 0
    client.events = [item(), item("incident:2")]
    await poller.poll_once()
    assert radio.sent
    assert db.traffic_get_item("flood:old")["active"]
    client.last_successful_feeds = {"incident", "flood"}
    client.last_errors = {}
    await poller.poll_once()
    assert db.traffic_get_item("flood:old")["missing_polls"] == 1
    assert not next(row for row in db.traffic_feed_status() if row["feed"] == "flood")["error"]
    db.close()


def test_traffic_near_border_uses_explicit_fallback_or_stays_unknown():
    prepared = prepare_councils([{"properties": {"lganame": "Tamworth Regional"}, "geometry": {
        "type": "Polygon", "coordinates": [[[150,-32],[152,-32],[152,-30],[150,-30]]]}}])
    assert match_traffic_council(item(lon=151, lat=-31), prepared).method == "approximate point"
    border = match_traffic_council(item(lon=150.0001, lat=-31), prepared)
    assert border.method == "provider council fallback" and "border" in border.reason
    unknown = match_traffic_council(item(lon=150.0001, lat=-31, council=""), prepared)
    assert not unknown.council and "border" in unknown.reason


def test_snapshot_age_does_not_infer_provider_timestamp_freshness():
    old = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    assert "Stale" in freshness(old, 5)
    assert "provider data freshness is not guaranteed" in freshness(datetime.now(timezone.utc).isoformat(), 5)
    assert "disabled" in freshness(old, 5, False)


@pytest.mark.parametrize("source,path,router", [("bom", "/bom", bom_router), ("rfs", "/rfs", rfs_router), ("traffic", "/traffic", traffic_router)])
def test_paginated_source_pages_expose_all_511_records_and_clamp_page(source, path, router):
    db = Database(":memory:")
    if source == "bom":
        db.replace_bom_current([{"id": f"item-{i:04}", "region": "NSW", "event": "Flood Warning", "headline": f"item-{i:04}"}
                                for i in range(511)], {"NSW"}, datetime.now(timezone.utc).isoformat())
    elif source == "rfs":
        for i in range(511):
            event = Incident(f"item-{i:04}", f"item-{i:04}", "Advice", "Sydney", "", "", "", "", "")
            db.rfs_save_incident(event, event.revision)
    else:
        for i in range(511):
            db.traffic_save_item(item(f"item-{i:04}", title=f"item-{i:04}"), "Tamworth Regional", True)
    app = FastAPI()
    app.include_router(router)
    app.state.db = db
    app.state.tx = TestRadio()
    app.state.poller = SimpleNamespace(status=SimpleNamespace(last_poll_result="ok", last_poll_time=""))
    app.state.rfs_poller = app.state.traffic_poller = SimpleNamespace(last_result="ok", last_poll="")
    client = TestClient(app)
    first, last = client.get(path), client.get(path+"?page=999")
    assert first.status_code == last.status_code == 200
    assert "of 511 items" in first.text and "Page 1 of 11" in first.text
    assert "Page 11 of 11" in last.text and "Showing 501" in last.text
    assert client.get(path+"?page=0").status_code == 422
    collected = set()
    for page in range(1, 12):
        rows, _ = db.current_listing(source, page)
        collected.update(row["alert_id" if source=="bom" else "incident_id" if source=="rfs" else "item_id"] for row in rows)
    assert len(collected) == 511
    db.close()


def test_rfs_size_agency_and_raw_update_are_preserved_for_display_and_radio():
    event = parse_incidents({"type": "FeatureCollection", "features": [{"properties": {
        "guid": "id", "title": "Fire", "category": "Watch and Act",
        "description": "COUNCIL AREA: Tamworth <br>SIZE: 14 ha <br>RESPONSIBLE AGENCY: Rural Fire Service <br>UPDATED: 3 Oct 2026 14:00"}}]})[0]
    assert event.size == "14 ha" and event.agency == "Rural Fire Service"
    text = " || ".join(format_incident(event, 126))
    assert "14 ha" in text and "Rural Fire Service" in text
    assert "14:00" not in text
    assert replace(event, size="20 ha").revision != event.revision


@pytest.mark.asyncio
async def test_failed_long_notice_completes_all_callbacks_without_attempting_the_remainder(monkeypatch):
    db = Database(":memory:")
    tx = TransmitManager(db)
    outcomes, calls = [], []
    tx.enqueue_notice([(f"part-{i}", 0) for i in range(41)],
                      lambda index, ok, error: outcomes.append((index, ok, error)))
    async def fail(part):
        calls.append(part.text)
        tx._stopped = True
        return False, "Source no longer selected"
    async def no_sleep(seconds):
        return None
    monkeypatch.setattr(tx, "_transmit_item", fail)
    monkeypatch.setattr("app.transmit.asyncio.sleep", no_sleep)
    await tx._worker()
    assert calls == ["part-0"] and len(outcomes) == 41 and not tx._queue
    assert all(not success for _, success, _ in outcomes)
    assert outcomes[-1][0] == 40 and "Not attempted" in outcomes[-1][2]
    db.close()


@pytest.mark.parametrize("change", ["identifier", "ended", "start"])
def test_malformed_traffic_items_cannot_advance_a_successful_snapshot(change):
    raw = payload()
    if change == "identifier":
        del raw["features"][0]["id"]
    elif change == "ended":
        raw["features"][0]["properties"]["ended"] = "false"
    else:
        raw["features"][0]["properties"]["start"] = "unknown"
    with pytest.raises(TrafficFeedError):
        parse_feed("incident", raw)


def test_legacy_database_keeps_records_when_new_columns_and_feed_status_are_added(tmp_path):
    path = str(tmp_path / "upgrade.db")
    db = Database(path)
    db.traffic_save_item(item(), "Tamworth Regional", True)
    db.close()
    # Simulate the previous schema's additive-column migration.
    import sqlite3
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE traffic_items DROP COLUMN normalized_data")
    connection.execute("ALTER TABLE bom_current DROP COLUMN provider_areas")
    connection.execute("ALTER TABLE rfs_incidents DROP COLUMN normalized_data")
    connection.execute("DROP TABLE traffic_feed_status")
    connection.close()
    db = Database(path)
    assert db.traffic_get_item("incident:1")["council"] == "Tamworth Regional"
    assert db.traffic_get_item("incident:1")["normalized_data"] == "{}"
    assert db.traffic_feed_status() == []
    db.close()
