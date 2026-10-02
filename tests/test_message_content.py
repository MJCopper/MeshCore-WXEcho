"""Radio wording keeps source facts and context through multipart splitting."""
from datetime import datetime
import re
from zoneinfo import ZoneInfo

from app.bom_enricher import WarningSection, parse_warning_api
from app.formatter import build_mesh_parts, frame_notice, marine_notice_sections
from app.models import Alert
from app.rfs.feed import Incident
from app.rfs.poller import format_incident
from app.traffic.feed import TrafficItem
from app.traffic.poller import format_item


def test_long_bom_locations_and_summary_survive_multipart_framing():
    locations = ", ".join(("Hunter", "Sydney", "Illawarra", "South Coast",
                           "Central Tablelands", "Northern Tablelands", "Riverina"))
    summary = ("Damaging winds and large hailstones are likely to produce heavy rainfall "
               "that may lead to flash flooding in low-lying roads and creek crossings "
               "throughout the affected districts.")
    enrichment = parse_warning_api({"warning": {"info": [{
        "summary": f"Locations which may be affected include {locations}. {summary}"}]}})
    assert enrichment.locations == locations
    assert summary.rstrip(".") in enrichment.summary
    alert = Alert("long-bom", "Severe Thunderstorm Warning", "", "NSW", "", "", "Alert",
                  specific_locations=enrichment.locations, warning_summary=enrichment.summary)
    content = build_mesh_parts(alert, max_bytes=126, split=False)
    content[0] = content[0].removeprefix(alert.event).lstrip()
    parts = frame_notice("BOM", "NEW", alert.event, content,
                         "check bom.gov.au", 126, alert.alert_id)
    joined = " || ".join(parts)
    assert len(parts) > 1
    assert all(part.startswith("BOM NEW ") and alert.event in part for part in parts)
    assert all(len(part.encode()) <= 126 for part in parts)
    assert all(area in joined for area in locations.split(", "))
    assert joined.count("Hunter") == 1
    assert "creek crossings" in joined
    assert joined.count("check bom.gov.au") == 1


def test_marine_parts_retain_section_type_full_date_and_shared_reference():
    alert = Alert("marine-1", "Marine Wind Warning", "", "NSW", "", "", "Alert",
                  warning_sections=(
                      WarningSection("Strong Wind Warning",
                                     "Hunter Coast, Sydney Coast, Illawarra Coast, Batemans Coast",
                                     "REN", "2026-09-30T06:00:00Z"),
                      WarningSection("Cancellation", "Eden Coast and Coffs Coast",
                                     "CAN", "2026-09-30T06:00:00Z"),
                  ))
    parts = frame_notice("BOM", "UPDATE", alert.event,
                         marine_notice_sections(alert, "Australia/Sydney", "UPDATE"),
                         "check bom.gov.au", 126, alert.alert_id)
    assert len(parts) > 1
    references = [re.search(r"#[0-9A-F]{4}", part).group() for part in parts]
    assert len(set(references)) == 1
    assert all(f"{index}/{len(parts)}" in part for index, part in enumerate(parts, 1))
    assert all("Wednesday 30 Sep 2026" in part for part in parts)
    assert all("Strong Wind Warning" in part for part in parts if "BOM UPDATE" in part)
    assert all("Marine Wind Warning" in part for part in parts if "BOM CANCELLED" in part)
    assert all(len(part.encode()) <= 126 for part in parts)
    text = " || ".join(parts)
    assert all(area in text for area in ("Hunter Coast", "Sydney Coast", "Illawarra Coast",
                                         "Batemans Coast", "Eden Coast", "Coffs Coast"))
    assert text.count("check bom.gov.au") == 1


def test_rfs_long_name_is_preserved_when_repeated_context_is_shortened():
    name = "Long Bushland Fire Near the Eastern Ridge and Upper Valley"
    incident = Incident("rfs-1", name, "Emergency Warning", "Tamworth Regional",
                        "Oxley Highway", "Not yet controlled", "Bush Fire", "", "")
    parts = format_incident(incident, 126, action="UPDATE")
    text = " || ".join(parts)
    assert len(parts) > 1
    assert all(part.startswith("NSW RFS UPDATE") for part in parts)
    assert all("Emergency Warning" in part for part in parts)
    assert "Eastern Ridge" in text and "Upper Valley" in text
    assert "Tamworth Regional council" in text
    assert all(f"{index}/{len(parts)}" in part for index, part in enumerate(parts, 1))
    assert parts[-1].endswith("check rfs.nsw.gov.au")
    assert all(len(part.encode()) <= 126 for part in parts)


def test_traffic_includes_advice_road_update_and_reliable_end_date():
    end = datetime(2026, 10, 3, 13, 0, tzinfo=ZoneInfo("Australia/Sydney")).timestamp()
    item = TrafficItem("incident:one", "incident", "CRASH", "Lane closed", "Oxley Highway",
                       "Tamworth", "", "Westbound", "One lane closed",
                       "Avoid the area; long delays expected", 151, -31, None, end, False, None)
    parts = format_item(item, "Tamworth Regional", 126, action="UPDATE")
    text = " || ".join(parts)
    assert len(parts) > 1
    assert all(part.startswith("Live Traffic NSW UPDATE") and "CRASH Oxley Highway" in part
               for part in parts)
    assert "One lane closed" in text
    assert "Avoid the area" in text and "long delays expected" in text
    assert "until Saturday 3 Oct 2026 1:00 PM" in text
    assert "Tamworth Regional council" in text
    assert parts[-1].endswith("check livetraffic.com")
    assert all(f"{index}/{len(parts)}" in part for index, part in enumerate(parts, 1))
    assert len(set(re.findall(r"#[0-9A-F]{4}", text))) == 1
    assert all(len(part.encode()) <= 126 for part in parts)
