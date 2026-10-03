"""NSW-only BOM collection, council selection, and migration."""
import pytest

from app.bom_area import match_councils
from app.db import Database
from app.filters import FilterRules
from app.poller import BomPoller
from app.models import Alert
from app.traffic.feed import prepare_councils


class Radio:
    message_budget = 195

    def enqueue(self, *args, **kwargs):
        raise AssertionError("dry-run should not send")


def warning(alert_id, area, region="NSW"):
    return {"id": alert_id, "region": region, "event": "Flood Warning",
            "headline": f"Flood Warning for {area}", "area_desc": area,
            "message_type": "Alert", "references": []}


def test_explicit_lga_and_polygon_council_matches():
    assert match_councils("Tamworth", (), ()).status == "unknown"
    assert match_councils("Tamworth Regional Council", (), ()).councils == ("Tamworth Regional",)
    assert match_councils("Hunter", (), ()).status == "unknown"
    assert match_councils("Tamworth and Hunter", (), ()).status == "unknown"
    boundaries = [{"properties": {"lganame": "Tamworth Regional"}, "geometry": {
        "type": "Polygon", "coordinates": [[[150, -32], [152, -32],
                                           [152, -30], [150, -30], [150, -32]]]}}]
    prepared = prepare_councils(boundaries)
    assert match_councils("NSW", (), ("-31,151 -31,151.5 -30.5,151.5 -30.5,151",),
                          prepared).councils == ("Tamworth Regional",)
    assert match_councils("NSW", (), ("-34,145 -34,146 -33,146 -33,145",),
                          prepared).status == "unknown"


@pytest.mark.asyncio
async def test_bom_council_filter_records_match_and_unknown_decisions():
    db = Database(":memory:")
    db.set_setting("bom_all_councils", False)
    db.set_setting("bom_councils", ["Tamworth Regional"])
    poller = BomPoller(db, Radio())
    rules = FilterRules([], ["Warning"], [])
    for alert_id, area in (("match", "Tamworth Regional Council"), ("other", "Campbelltown Council"),
                           ("broad", "Hunter")):
        await poller._process(warning(alert_id, area), rules, "Australia/Sydney", 0, True)
    rows = {row["external_id"]: row for row in db.query_service_history(source="bom")}
    assert rows["match"]["transmit_status"] == "dry-run"
    assert rows["match"]["metadata"]["matched_councils"] == ["Tamworth Regional"]
    assert rows["other"]["disposition"] == "filtered"
    assert rows["broad"]["transmit_status"] == "dry-run"
    assert rows["broad"]["metadata"]["council_match"] == "unknown"
    db.set_setting("bom_include_unknown_councils", False)
    await poller._process(warning("strict", "Hunter"), rules, "Australia/Sydney", 0, True)
    assert db.latest_history("strict")["disposition"] == "filtered"
    db.close()


@pytest.mark.asyncio
async def test_poller_uses_nsw_feed_even_with_retired_region_setting(monkeypatch):
    db = Database(":memory:")
    db.set_setting("bom_regions", ["VIC"])
    calls = []

    class Client:
        last_errors = []
        last_successful_regions = {"NSW"}
        last_server_date = None

        async def fetch_active(self, regions, districts=None):
            calls.append(regions)
            return [warning("nsw", "Hunter"), warning("vic", "Victoria", "VIC")], "rss"

    monkeypatch.setattr("app.poller.BOMClient", Client)
    poller = BomPoller(db, Radio())
    await poller.poll_once()
    assert calls == [["NSW"]]
    assert [row["alert_id"] for row in db.bom_current_items(["NSW", "VIC"])] == ["nsw"]
    assert {row["external_id"] for row in db.query_service_history(source="bom")} == {"nsw"}
    db.close()


def test_reopen_removes_proven_non_nsw_data_but_keeps_ambiguous_history(tmp_path):
    path = str(tmp_path / "wx-echo.db")
    db = Database(path)
    db.set_setting("bom_regions", ["NSW", "VIC"])
    db.replace_bom_current([warning("nsw", "Hunter")], {"NSW"}, "2026-10-01T00:00:00+00:00")
    with db._lock:
        db._conn.execute("INSERT INTO bom_current(region,alert_id,event,headline,area,issued,expires,message_type,source_url,fetched_at) VALUES ('VIC','vic','Flood Warning','VIC warning','Victoria','','','Alert','','')")
        db._conn.execute("INSERT INTO bom_feed_snapshots(region,fetched_at) VALUES ('VIC','')")
        db._conn.commit()
    db.add_history("https://example.com/vic/warning", "Flood Warning", "Victoria", "sent")
    db.add_history("https://reg.bom.gov.au/products/IDV21037.shtml", "Flood Warning", "Victoria", "sent")
    db.add_history("legacy-unknown", "Flood Warning", "Victoria", "sent")
    db.add_history("nsw", "Flood Warning", "Hunter", "sent", metadata={"region": "NSW"})
    db.close()
    reopened = Database(path)
    assert reopened.get_setting("bom_regions") is None
    assert reopened.bom_current_items(["VIC"]) == []
    assert reopened.bom_snapshot_regions(["VIC"]) == []
    ids = {row["external_id"] for row in reopened.query_service_history(source="bom")}
    assert ids == {"legacy-unknown", "nsw"}
    reopened.close()


@pytest.mark.asyncio
async def test_newly_selected_council_reconsiders_existing_warning():
    db = Database(":memory:")
    db.set_setting("bom_all_councils", False)
    db.set_setting("bom_councils", ["Campbelltown"])
    poller = BomPoller(db, Radio())
    rules = FilterRules([], ["Warning"], [])
    alert = warning("coverage", "Tamworth Regional Council")
    await poller._process(alert, rules, "Australia/Sydney", 0, True)
    assert db.latest_history("coverage")["disposition"] == "filtered"
    db.set_setting("bom_councils", ["Campbelltown", "Tamworth Regional"])
    await poller._process(alert, rules, "Australia/Sydney", 0, True)
    rows = [row for row in db.query_service_history(source="bom")
            if row["external_id"] == "coverage"]
    assert len(rows) == 2
    assert rows[0]["transmit_status"] == "dry-run"
    db.close()


@pytest.mark.asyncio
async def test_expanding_selected_councils_reconsiders_previously_sent_warning():
    db = Database(":memory:")
    db.set_setting("bom_all_councils", False)
    db.set_setting("bom_councils", ["Tamworth Regional"])
    poller = BomPoller(db, Radio())
    rules = FilterRules([], ["Warning"], [])
    alert = warning("multi-council", "Tamworth Regional Council and Campbelltown Council")
    await poller._process(alert, rules, "Australia/Sydney", 0, True)
    db.upsert_state(alert_id="multi-council", event="Flood Warning",
                    headline=alert["headline"], expires="",
                    msg_hash=Alert.from_bom(alert).content_hash(),
                    disposition="sent", sent_ts="2026-10-01T00:00:00+00:00")
    db.set_setting("bom_councils", ["Tamworth Regional", "Campbelltown"])
    await poller._process(alert, rules, "Australia/Sydney", 0, True)
    rows = [row for row in db.query_service_history(source="bom")
            if row["external_id"] == "multi-council"]
    assert len(rows) == 2
    assert rows[0]["disposition"] == "update"
    assert rows[0]["transmit_status"] == "dry-run"
    db.close()


@pytest.mark.asyncio
async def test_cancellation_of_sent_warning_is_not_lost_when_area_is_unknown():
    db = Database(":memory:")
    db.set_setting("bom_all_councils", False)
    db.set_setting("bom_councils", ["Tamworth Regional"])
    db.set_setting("bom_include_unknown_councils", False)
    db.upsert_state(alert_id="cancel", event="Flood Warning",
                    headline="Flood Warning", expires="", msg_hash="previous",
                    disposition="sent", sent_ts="2026-10-01T00:00:00+00:00")
    poller = BomPoller(db, Radio())
    item = warning("cancel", "Hunter")
    item["message_type"] = "Cancel"
    await poller._process(item, FilterRules([], ["Warning"], []),
                          "Australia/Sydney", 0, True)
    row = db.latest_history("cancel")
    assert row["disposition"] == "cancelled"
    assert row["transmit_status"] == "dry-run"
    db.close()
