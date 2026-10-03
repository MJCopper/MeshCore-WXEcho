from dataclasses import replace

import pytest

from app.brief import NoticeTooLong, brief_parts, brief_bom_parts
from app.models import Alert
from app.rfs.feed import Incident
from app.rfs.poller import format_incident
from app.traffic.feed import TrafficItem
from app.traffic.poller import format_item


def framed(locations="Town", core="Risk: damaging winds", optional=()):
    return brief_parts("BOM", "NEW", "Weather Warning", [(core, locations)],
                       optional, "check bom.gov.au", 126, "brief")


def readable(parts):
    import re
    return " ".join(re.sub(r"^\d+/\d+ ", "", p) for p in parts)


def test_long_description_never_adds_a_third_part():
    parts = framed(optional=["Descriptive provider detail " * 100])
    assert len(parts) <= 2
    assert "damaging winds" in readable(parts)
    assert "Town" in readable(parts)
    assert "Descriptive" not in readable(parts)


def test_location_exception_preserves_all_names_and_drops_optional_detail():
    names = [f"District {i:02} Valley" for i in range(12)]
    parts = framed(", ".join(names), optional=["Unnecessary narrative"])
    assert len(parts) == 3
    text = readable(parts)
    assert all(name in text for name in names)
    assert "Unnecessary narrative" not in text
    assert all(len(p.encode()) <= 126 for p in parts)
    assert all(p.startswith(f"{i}/3 ") for i, p in enumerate(parts, 1))
    assert text.count("BOM NEW") == 1


@pytest.mark.parametrize("locations,core", [
    (", ".join(f"District {i} Valley" for i in range(40)), "Risk: damaging winds"),
    ("Town", "Mandatory cause or status " * 13),
])
def test_overflow_is_blocked_without_silent_truncation(locations, core):
    with pytest.raises(NoticeTooLong, match="Formatting blocked"):
        framed(locations, core)


def test_unicode_location_and_postcode_remain_intact():
    parts = framed("WERRIS CREEK RD, QUIPOLLY 2343; Café Valley", optional=["Brief detail"])
    assert "QUIPOLLY 2343" in readable(parts)
    assert "Café Valley" in readable(parts)
    assert all(len(p.encode()) <= 126 for p in parts)


def test_bom_cause_and_cancellation_scope_survive_briefing():
    alert = Alert("brief-bom", "Severe Thunderstorm Warning", "", "", "", "", "Alert",
                  specific_locations="Orange, Bowral",
                  warning_summary="Severe thunderstorms are likely to produce damaging winds, large hailstones and heavy rainfall that may lead to flash flooding in the warning area over the next several hours. Locations which may be affected include Orange and Bowral. Severe thunderstorms are no longer occurring in the South West Slopes district and the warning for this district is CANCELLED.")
    parts = brief_bom_parts(alert, "Australia/Sydney", "UPDATE", 126)
    text = readable(parts)
    assert len(parts) <= 2
    assert "CANCELLED" in parts[0]
    assert text.index("South West Slopes") < text.index("Orange")
    for fact in ("Orange", "Bowral", "damaging winds", "large hail", "heavy rain", "flash flooding"):
        assert fact in text
    assert "over the next several hours" not in text


def test_bom_forecast_preserves_locations_after_each_time():
    alert = Alert("forecast", "Thunderstorm Warning", "", "", "", "", "Alert",
                  specific_locations="Sydney",
                  warning_summary="Thunderstorms likely to produce damaging winds were detected near Bargo. They are forecast to affect Picton and Blackheath by 3:35 pm and Camden and Penrith by 4:05 pm.")
    parts = brief_bom_parts(alert, "Australia/Sydney", "NEW", 126)
    text = readable(parts)
    assert all(name in text for name in ("Sydney", "Bargo", "Picton", "Blackheath", "Camden", "Penrith"))
    assert len(parts) <= 2


def test_negated_bom_hazard_is_not_inverted():
    alert = Alert("negative", "Thunderstorm Warning", "", "", "", "", "Alert",
                  specific_locations="Town", warning_summary="Damaging winds are not expected.")
    assert "not expected" in readable(brief_bom_parts(alert, "", "NEW", 126))


def test_rfs_descriptive_fields_do_not_add_parts():
    incident = Incident("rfs-short", "Road, Town", "Advice", "Council",
                        "Road, Town 2343", "Being controlled", "Grass Fire",
                        "Reported size " * 100, "Agency " * 100)
    parts = format_incident(incident, 126)
    text = readable(parts)
    assert len(parts) <= 2
    assert all(fact in text for fact in ("Advice", "Grass Fire", "Town 2343", "Being controlled"))


def test_traffic_extra_advice_does_not_add_parts():
    item = TrafficItem("short", "incident", "CRASH", "Crash", "Highway",
                       "Town", "", "Northbound", "Road closed", "Detailed advice " * 100,
                       None, None, None, None, False, None, road_details="between First Road and Second Road")
    parts = format_item(item, "", 126)
    text = readable(parts)
    assert len(parts) <= 2
    assert all(fact in text for fact in ("CRASH", "Highway", "Town", "First Road", "Second Road", "Road closed"))
    assert "Detailed advice" not in text


@pytest.mark.asyncio
async def test_bom_formatting_block_is_logged_once_without_queuing_or_delivery_state():
    from test_poller_multipart import _FakeDb, _FakeTx, _warning_item
    from app.filters import FilterRules
    from app.poller import BomPoller
    db, tx = _FakeDb(), _FakeTx()
    tx.message_budget = 126
    poller = BomPoller(db, tx)
    item = {**_warning_item("too-long"), "area_desc": ", ".join(f"District {i} Valley" for i in range(50))}
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    for _ in range(2):
        await poller._process(item, rules, "Australia/Sydney", 0, False)
    assert tx.enqueued == [] and db.state_rows == []
    assert len(db.history_rows) == 1 and len(db.errors) == 1
    assert db.history_rows[0]["transmit_status"] == "blocked"
    assert db.history_rows[0]["disposition"] == "formatting-blocked"


@pytest.mark.asyncio
async def test_rfs_blocked_notice_does_not_prevent_other_notices_and_is_not_repeated():
    from test_rfs import FakeTx, FakeClient, incident
    from app.db import Database
    from app.rfs.poller import RFSPoller
    db = Database(":memory:")
    db.set_setting("rfs_enabled", True)
    db.set_setting("rfs_councils", ["Central Coast"])
    db.set_setting("rfs_levels", ["Watch and Act"])
    db.set_setting("dry_run", False)
    tx = FakeTx()
    tx.message_budget = 126
    bad = replace(incident("bad"), location=", ".join(f"District {i} Valley" for i in range(50)))
    poller = RFSPoller(db, tx, FakeClient([bad, incident("good")]))
    await poller.poll_once()
    assert db.rfs_latest_history("bad")["transmit_status"] == "blocked"
    assert tx.sent and "Bushland Road" in " ".join(row[0] for row in tx.sent)
    await poller.poll_once()
    assert len([r for r in db.rfs_history() if r["external_id"] == "bad"]) == 1
    assert not db.rfs_get_incident("bad")["last_sent_hash"]
    db.close()


@pytest.mark.asyncio
async def test_traffic_blocked_notice_does_not_prevent_other_notices_and_is_not_repeated():
    from test_traffic import Tx, Client, item, configure
    from app.db import Database
    from app.traffic.poller import TrafficPoller
    db = Database(":memory:")
    configure(db)
    db.set_setting("traffic_baseline_done", True)
    db.set_setting("traffic_all_councils", True)
    # A new baseline can be required per feed; record the existing live-feed baseline.
    db.set_setting("traffic_baseline_feeds", ["incident"])
    tx = Tx()
    tx.message_budget = 126
    bad = item("incident:bad", road_details=", ".join(f"District {i} Valley" for i in range(50)))
    poller = TrafficPoller(db, tx, Client([bad, item("incident:good")]))
    await poller.poll_once()
    assert db.latest_service_history("traffic", bad.item_id)["transmit_status"] == "blocked"
    assert tx.sent
    await poller.poll_once()
    assert len([r for r in db.query_service_history(source="traffic") if r["external_id"] == bad.item_id]) == 1
    assert not db.traffic_get_item(bad.item_id)["last_sent_hash"]
    db.close()


def test_location_named_snowy_mountains_does_not_infer_snow_hazard():
    alert = Alert("snowy-place", "Thunderstorm Warning", "", "", "", "", "Alert",
                  specific_locations="Snowy Mountains",
                  warning_summary="Damaging winds are likely. Locations which may be affected include Snowy Mountains.")
    text = readable(brief_bom_parts(alert, "", "NEW", 126))
    assert "Risk: damaging winds" in text
    assert "snow" not in text.replace("Snowy Mountains", "").casefold()
