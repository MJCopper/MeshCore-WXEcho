"""Regression cases found by reviewing provider-to-display/radio data flow."""
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bom import BOMError, parse_rss
from app.bom_enricher import BOMEnrichment, parse_warning_api
from app.db import Database
from app.dedupe import decide
from app.filters import FilterRules, should_include
from app.formatter import _to_local
from app.models import Alert
from app.poller import BomPoller
from app.rfs.feed import council_key
from app.rfs.poller import RFSPoller
from app.rfs.feed import Incident
from app.traffic.feed import TrafficFeedError, parse_feed
from app.traffic.poller import TrafficPoller, format_item
from app.traffic.web import router as traffic_router
from app.transmit import TransmitManager
from app.web.routes import router as bom_router


@pytest.mark.parametrize("provider,canonical", [
    ("Tamworth Regional Council", "Tamworth Regional"),
    ("Central Darling Shire Council", "Central Darling"),
    ("Woollahra Municipal Council", "Woollahra"),
    ("Council of the City of Sydney", "Sydney"),
    ("Council of the City of Parramatta", "City Of Parramatta"),
    ("City of Lithgow Council", "Lithgow City"),
    ("The Council of the Municipality of Kiama", "Kiama"),
    ("MidCoast Council", "Mid-Coast"),
])
def test_provider_council_names_match_canonical(provider, canonical):
    assert council_key(provider) == council_key(canonical)


def test_exclusion_matches_qualified_product_family():
    assert not should_include("Major Flood Warning", FilterRules([], ["Warning"], ["Flood Warning"]))


@pytest.mark.parametrize("raw", ["<html><body>Unavailable</body></html>", "<rss/>",
                                     "<rss><channel><item><title>Flood Warning</title></item></channel></rss>"])
def test_invalid_feed_cannot_become_empty_successful_snapshot(raw):
    with pytest.raises(BOMError):
        parse_rss(raw)


def test_guid_without_link_does_not_invent_a_source_reference():
    item = parse_rss("<rss><channel><item><title>Flood Warning</title><guid>id</guid></item></channel></rss>")[0]
    assert item["references"] == []


def alert(**kwargs):
    return replace(Alert("id", "Flood Warning", "Flood Warning", "Tamworth", "", "", "Alert"), **kwargs)


@pytest.mark.parametrize("change", [
    {"specific_locations": "Oxley Highway"}, {"warning_summary": "Major flooding expected"},
    {"onset": "2026-10-03T02:00:00Z"}, {"ends": "2026-10-03T05:00:00Z"},
])
def test_enriched_content_and_hazard_time_changes_trigger_update(change):
    old = alert()
    state = {"disposition": "sent", "msg_hash": old.content_hash()}
    result = decide(alert(**change), FilterRules([], ["Warning"], []), lambda _: state)
    assert result.transmit and result.disposition == "update"
    assert alert(**change).revision_hash() != old.revision_hash()


def test_referenced_update_detects_changed_details_with_same_headline():
    old = alert()
    state = {"disposition": "sent", "msg_hash": old.content_hash(), "headline": old.headline, "expires": ""}
    result = decide(alert(alert_id="new", references=["id"], detail="New affected roads"),
                    FilterRules([], ["Warning"], []), lambda key: state if key == "id" else None)
    assert result.transmit and result.disposition == "update"


def test_sent_cancellation_survives_changed_product_selection_and_does_not_repeat():
    cancel = alert(message_type="Cancel", references=["previous"])
    original = {"disposition": "sent", "msg_hash": "old"}
    rules = FilterRules([], [], [])
    assert decide(cancel, rules, lambda key: original if key == "previous" else None).transmit
    sent = {"disposition": "cancelled", "msg_hash": cancel.content_hash()}
    assert not decide(cancel, rules, lambda key: sent if key == "id" else original).transmit


def test_bom_retains_all_distinct_api_summaries():
    result = parse_warning_api({"warning": {"info": [
        {"summary": "Damaging winds likely to produce damage"},
        {"summary": "Flooding may close Oxley Highway"},
        {"summary": "Flooding may close Oxley Highway"},
    ]}})
    assert "Damaging winds" in result.summary and "Oxley Highway" in result.summary
    assert result.summary.count("Oxley Highway") == 1


class Radio:
    message_budget = 126
    def enqueue(self, *args, **kwargs):
        raise AssertionError("Review must not send to a radio")


@pytest.mark.asyncio
async def test_bom_duplicates_remain_included_and_enriched_content_is_presented():
    db = Database(":memory:")
    poller = BomPoller(db, Radio())
    class Enricher:
        async def enrich(self, url):
            return BOMEnrichment(locations="Oxley Highway", summary="Major flooding expected")
    poller._enricher = Enricher()
    item = {"id": "id", "region": "NSW", "event": "Flood Warning", "headline": "Flood Warning",
            "area_desc": "Tamworth", "references": ["https://www.bom.gov.au/test"]}
    rules = FilterRules([], ["Warning"], [])
    await poller._process(item, rules, "Australia/Sydney", 0, True)
    sent = alert(specific_locations="Oxley Highway", warning_summary="Major flooding expected")
    db.upsert_state(alert_id="id", event=sent.event, headline=sent.headline, expires="",
                    msg_hash=sent.content_hash(), disposition="sent", sent_ts="2026-10-03T00:00:00Z")
    await poller._process(item, rules, "Australia/Sydney", 0, True)
    assert item["selection"] == "included"
    db.replace_bom_current([item], {"NSW"}, "2026-10-03T00:00:00Z")
    app = FastAPI()
    app.include_router(bom_router)
    app.state.db, app.state.poller, app.state.tx = db, poller, Radio()
    page = TestClient(app).get("/bom")
    assert "Oxley Highway" in page.text and "Major flooding expected" in page.text
    db.close()


@pytest.mark.asyncio
async def test_district_filter_retains_excluded_warning_in_current_feed(monkeypatch):
    db = Database(":memory:")
    db.set_setting("bom_districts", ["Hunter"])
    class Client:
        last_errors = []
        last_successful_regions = {"NSW"}
        last_server_date = None
        async def fetch_active(self, regions, districts=None):
            assert not districts
            return [{"region": "NSW", "id": "id", "event": "Flood Warning", "area_desc": "Tamworth"}], "rss"
    monkeypatch.setattr("app.poller.BOMClient", Client)
    await BomPoller(db, Radio()).poll_once()
    assert db.bom_current_items(["NSW"])[0]["selection"] == "excluded"
    assert "districts" in db.latest_history("id")["detail"]
    db.close()


def traffic_payload(**changes):
    props = {"displayName": "CRASH", "mainCategory": "CRASH", "roads": [
        {"mainStreet": "Oxley Highway", "suburb": "Tamworth", "crossStreet": "Bridge Street",
         "locationQualifier": "at", "impactedLanes": [{"extent": "Closed", "description": "One lane"}]}],
        "adviceA": "Avoid area", "adviceB": "Exercise caution", "adviceC": "Expect delays",
        "otherAdvice": "<p>Emergency access only</p>", "diversions": "Use bypass"}
    props.update(changes)
    return {"type": "FeatureCollection", "lastPublished": int(time.time() * 1000),
            "features": [{"id": 1, "geometry": {"type": "Point", "coordinates": [151, -31]}, "properties": props}]}


def test_traffic_preserves_advice_and_cross_street_in_byte_capped_messages():
    item = parse_feed("incident", traffic_payload())[0]
    text = " || ".join(format_item(item, "Tamworth Regional", 126))
    for fact in ("Bridge Street", "Closed", "One lane", "Avoid area", "Exercise caution",
                 "Expect delays", "Emergency access only", "Use bypass"):
        assert fact in text
    assert all(len(part.encode()) <= 126 for part in format_item(item, "Tamworth Regional", 126))
    changed = parse_feed("incident", traffic_payload(adviceB="Road closed"))[0]
    assert changed.revision != item.revision


def test_hidden_traffic_end_is_not_broadcast():
    item = parse_feed("incident", traffic_payload(end=int((time.time()+3600)*1000), hideEndDate=True))[0]
    assert "until " not in " || ".join(format_item(item, "Tamworth Regional", 126))


def test_nonfinite_traffic_publication_is_rejected():
    payload = traffic_payload()
    payload["lastPublished"] = float("nan")
    with pytest.raises(TrafficFeedError):
        parse_feed("incident", payload)


def test_naive_timestamp_is_not_interpreted_using_the_host_timezone():
    assert _to_local("2026-10-03T14:00:00", "Australia/Sydney") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["rfs", "traffic"])
async def test_disabling_service_during_fetch_prevents_processing(service):
    db = Database(":memory:")
    db.set_setting(service + "_enabled", True)
    db.set_setting(service + "_all_councils", True)
    class Client:
        async def fetch(self):
            db.set_setting(service + "_enabled", False)
            return []
        async def boundaries(self):
            return []
    poller = (RFSPoller if service == "rfs" else TrafficPoller)(db, Radio(), Client())
    await poller.poll_once()
    assert poller.last_result == "disabled"
    assert not poller.last_successful_poll
    db.close()


@pytest.mark.asyncio
async def test_queued_messages_stop_when_dry_run_is_enabled(monkeypatch):
    db = Database(":memory:")
    db.set_setting("dry_run", False)
    tx = TransmitManager(db)
    tx.enqueue("previously queued")
    db.set_setting("dry_run", True)
    async def must_not_send(*args):
        raise AssertionError("Radio was called during Dry Run")
    monkeypatch.setattr(tx, "_try_send", must_not_send)
    ok, reason = await tx._transmit_item(tx._queue.popleft())
    assert not ok and "Dry Run" in reason
    db.close()


def test_traffic_page_shows_normalized_impact_and_advice():
    db = Database(":memory:")
    item = parse_feed("incident", traffic_payload())[0]
    db.traffic_save_item(item, "Tamworth Regional", True)
    app = FastAPI()
    app.include_router(traffic_router)
    app.state.db = db
    app.state.tx = Radio()
    app.state.traffic_poller = SimpleNamespace(last_result="ok", last_poll="")
    page = TestClient(app).get("/traffic")
    assert page.status_code == 200
    assert "Bridge Street" in page.text and "Emergency access only" in page.text
    db.close()


@pytest.mark.asyncio
async def test_selected_flood_copy_is_not_hidden_by_unselected_incident_copy():
    db = Database(":memory:")
    db.set_setting("traffic_enabled", True)
    db.set_setting("traffic_all_councils", True)
    db.set_setting("traffic_types", ["flood"])
    payload = traffic_payload()
    payload["features"][0]["properties"]["OrgName"] = "Tamworth Regional Council"
    incident = replace(parse_feed("incident", payload)[0], council="Tamworth Regional Council")
    flood = replace(incident, item_id="flood:1", feed="flood", council="Tamworth Regional Council")
    class Client:
        async def fetch(self):
            return [incident, flood]
        async def boundaries(self):
            return []
    await TrafficPoller(db, Radio(), Client()).poll_once()
    assert db.latest_service_history("traffic", "incident:1")["disposition"] == "excluded-hazard-type"
    assert db.latest_service_history("traffic", "flood:1")["transmit_status"] == "dry-run"
    db.set_setting("traffic_types", ["incident", "flood"])
    await TrafficPoller(db, Radio(), Client()).poll_once()
    assert db.latest_service_history("traffic", "incident:1")["disposition"] == "excluded-duplicate-provider-item"
    db.close()


def test_rfs_missing_incident_leaves_current_view_after_two_successful_polls():
    db = Database(":memory:")
    incident = Incident("id", "Fire", "Advice", "Tamworth", "Road", "Under control", "Bush Fire", "", "")
    db.rfs_save_incident(incident, incident.revision)
    assert len(db.rfs_active_incidents("")) == 1
    db.rfs_missing_after_poll(set())
    assert len(db.rfs_active_incidents("")) == 1
    db.rfs_missing_after_poll(set())
    assert not db.rfs_active_incidents("")
    assert db.rfs_get_incident("id") is not None
    db.close()


def test_feed_issue_time_is_not_normalized_as_hazard_onset():
    item = parse_rss("<rss><channel><item><title>Flood Warning</title><guid>id</guid>"
                     "<pubDate>Sat, 03 Oct 2026 00:00:00 GMT</pubDate></item></channel></rss>")[0]
    parsed = Alert.from_bom(item)
    assert parsed.effective == "2026-10-03T00:00:00+00:00"
    assert parsed.onset == ""


class GuardRadio:
    message_budget = 126
    supports_notice_guards = True
    def __init__(self):
        self.guards = []
    def enqueue_notice(self, parts, on_result=None, priority=3, valid_if=None):
        self.guards.append(valid_if)
        return True
    def enqueue_verification(self, *args, **kwargs):
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["bom", "rfs", "traffic"])
async def test_queued_notice_rechecks_council_selection_and_service_enable(service):
    db = Database(":memory:")
    db.set_setting("dry_run", False)
    db.set_setting(service + "_enabled", True)
    db.set_setting(service + "_all_councils", True)
    radio = GuardRadio()
    if service == "bom":
        poller = BomPoller(db, radio)
        await poller._process({"id": "id", "event": "Flood Warning", "area_desc": "Tamworth Regional Council"},
                              FilterRules([], ["Warning"], []), "Australia/Sydney", 0, False)
    else:
        if service == "rfs":
            event = Incident("id", "Fire", "Watch and Act", "Tamworth", "Road", "Under control", "Bush Fire", "", "")
        else:
            db.set_setting("traffic_baseline_done", True)
            event = replace(parse_feed("incident", traffic_payload())[0], council="Tamworth")
        class Client:
            async def fetch(self):
                return [event]
            async def boundaries(self):
                return []
        await (RFSPoller if service == "rfs" else TrafficPoller)(db, radio, Client()).poll_once()
    assert radio.guards and radio.guards[0]()
    db.set_setting(service + "_all_councils", False)
    db.set_setting(service + "_councils", ["Campbelltown"])
    assert not radio.guards[0]()
    db.set_setting(service + "_all_councils", True)
    assert radio.guards[0]()
    db.set_setting(service + "_enabled", False)
    assert not radio.guards[0]()
    db.close()


@pytest.mark.asyncio
async def test_bom_queued_notice_stops_after_disappearing_from_later_feed():
    db = Database(":memory:")
    db.set_setting("dry_run", False)
    radio = GuardRadio()
    poller = BomPoller(db, radio)
    await poller._process({"id": "id", "event": "Flood Warning", "area_desc": "Tamworth"},
                          FilterRules([], ["Warning"], []), "Australia/Sydney", 0, False)
    assert radio.guards[0]()
    db.replace_bom_current([], {"NSW"}, "2099-01-01T00:00:00+00:00")
    assert not radio.guards[0]()
    db.close()


@pytest.mark.asyncio
async def test_bom_product_selection_change_updates_excluded_history():
    db = Database(":memory:")
    poller = BomPoller(db, Radio())
    item = {"id": "id", "region": "NSW", "event": "Flood Watch", "area_desc": "Tamworth"}
    await poller._process(item, FilterRules([], ["Warning"], []), "Australia/Sydney", 0, True)
    assert db.latest_history("id")["disposition"] == "filtered"
    await poller._process(item, FilterRules(["Flood Watch"], ["Warning"], []), "Australia/Sydney", 0, True)
    assert db.latest_history("id")["transmit_status"] == "dry-run"
    assert "Flood Watch" in db.latest_history("id")["transmitted_text"]
    db.close()


@pytest.mark.asyncio
async def test_bom_api_expiry_is_presented_and_blocks_stale_warning():
    db = Database(":memory:")
    poller = BomPoller(db, Radio())
    class Enricher:
        async def enrich(self, url):
            return BOMEnrichment(issued="2000-01-01T00:00:00Z", expires="2000-01-02T00:00:00Z")
    poller._enricher = Enricher()
    item = {"id": "id", "event": "Flood Warning", "area_desc": "Tamworth",
            "references": ["https://www.bom.gov.au/test"]}
    await poller._process(item, FilterRules([], ["Warning"], []), "Australia/Sydney", 0, True)
    assert item["effective"] == "2000-01-01T00:00:00Z"
    assert item["expires"] == "2000-01-02T00:00:00Z"
    assert item["selection"] == "excluded"
    assert "expired" in db.latest_history("id")["detail"]
    db.close()


@pytest.mark.parametrize("payload", [[], {"warning": []}, {"warning": {"info": ["invalid"]}}])
def test_invalid_api_shape_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_warning_api(payload)
