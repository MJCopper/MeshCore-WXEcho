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
    assert "BOM NEW" in parts[0] and alert.event in parts[0]
    assert joined.count(alert.event) == 1
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
    references = re.findall(r"#[0-9A-F]{4}", " ".join(parts))
    assert len(references) == 1
    assert len(set(references)) == 1
    assert all(part.startswith(f"{index}/{len(parts)} ") for index, part in enumerate(parts, 1))
    assert "Wednesday 30 Sep 2026" in " ".join(parts)
    assert "CANCELLED Marine Wind Warning" in " ".join(parts)
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
    assert "NSW RFS UPDATE" in parts[0]
    assert text.count("NSW RFS") == 1
    assert text.count("Emergency Warning") == 1
    assert "Eastern Ridge" in text and "Upper Valley" in text
    assert "Tamworth Regional council" in text
    assert all(part.startswith(f"{index}/{len(parts)} ") for index, part in enumerate(parts, 1))
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
    assert "Live Traffic NSW UPDATE" in parts[0]
    assert text.count("Live Traffic NSW") == 1
    assert text.count("CRASH Oxley Highway") == 1
    assert "One lane closed" in text
    assert "Avoid the area" in text and "long delays expected" in text
    assert "until Saturday 3 Oct 2026 1:00 PM" in text
    assert "Tamworth Regional council" in text
    assert parts[-1].endswith("check livetraffic.com")
    assert all(part.startswith(f"{index}/{len(parts)} ") for index, part in enumerate(parts, 1))
    assert len(set(re.findall(r"#[0-9A-F]{4}", text))) == 1
    assert all(len(part.encode()) <= 126 for part in parts)


def test_first_part_only_context_preserves_unicode_content_and_large_numbering():
    content = "; ".join(f"Location {i}: café, road closed" for i in range(100))
    parts = frame_notice("Live Traffic NSW", "UPDATE", "Flood", [content],
                         "check livetraffic.com", 126, "large-notice")
    assert len(parts) > 9
    assert all(part.startswith(f"{i}/{len(parts)} ") for i, part in enumerate(parts, 1))
    assert all(len(part.encode()) <= 126 for part in parts)
    text = " ".join(parts)
    assert text.count("Live Traffic NSW") == 1
    assert len(re.findall(r"#[0-9A-F]{4}", text)) == 1
    assert text.count("café") == 100
    assert all(f"Location {i}:" in text for i in range(100))
    assert parts[-1].endswith("check livetraffic.com")


def test_bom_removes_repeated_location_sentence_after_hazard_description():
    alert = Alert("repeat", "Severe Thunderstorm Warning", "", "NSW", "", "", "Alert",
                  specific_locations="Orange, Goulburn, Dubbo, Nowra, Bowral and Bathurst",
                  warning_summary="Damaging winds and heavy rainfall. Locations which may be affected include Orange, Goulburn, Dubbo, Nowra, Bowral and Bathurst.")
    text = " ".join(build_mesh_parts(alert, split=False))
    assert text.count("Orange") == 1
    assert "Damaging winds and heavy rainfall" in text


def test_bom_preserves_additional_locations_and_qualified_lists():
    alert = Alert("additional", "Flood Warning", "", "NSW", "", "", "Alert",
                  specific_locations="Orange", warning_summary="Heavy rainfall. Locations which may be affected include Orange and Goulburn.")
    assert "Goulburn" in " ".join(build_mesh_parts(alert, split=False))


def test_mixed_thunderstorm_cancellation_leads_with_affected_scope():
    from app.formatter import bom_notice_sections
    alert = Alert("mixed", "Severe Thunderstorm Warning", "", "NSW", "", "", "Alert",
                  specific_locations="Orange, Parkes, Blayney, Trunkey Creek and Taralga",
                  warning_summary="Damaging winds are likely. Locations which may be affected include Orange, Parkes, Blayney, Trunkey Creek and Taralga. Severe thunderstorms are no longer occurring in the Snowy Mountains and Australian Capital Territory districts and the warning for these districts is CANCELLED.")
    sections = bom_notice_sections(alert, "Australia/Sydney", "UPDATE")
    parts = frame_notice("BOM", "UPDATE", alert.event, sections, "check bom.gov.au", 126, alert.alert_id)
    text = " ".join(parts)
    assert "CANCELLED —" in parts[0]
    assert text.count("CANCELLED") == 1
    assert "ACTIVE WARNING —" in text
    assert text.index("Snowy Mountains") < text.index("Orange")
    assert text.count("Orange") == 1
    assert all(len(p.encode()) <= 126 for p in parts)
    assert not any(p.endswith("Blayney,") and len(p.encode()) < 30 for p in parts)


def test_long_clause_fills_a_short_preceding_location_remainder():
    from app.formatter import _split_plain
    parts = _split_plain("Blayney, Trunkey Creek and Taralga. " + "Damaging winds and heavy rainfall " * 5, 126)
    assert parts[0] != "Blayney,"
    assert len(parts[0].encode()) > 80
    assert all(len(p.encode()) <= 126 for p in parts)


def test_negated_cancellation_is_not_promoted_to_cancelled_section():
    from app.formatter import bom_notice_sections
    alert = Alert("not-cancelled", "Flood Warning", "", "NSW", "", "", "Alert",
                  specific_locations="Hunter", warning_summary="This warning is not CANCELLED.")
    sections = bom_notice_sections(alert, "Australia/Sydney", "UPDATE")
    assert all(not isinstance(section, tuple) for section in sections)
    assert "not CANCELLED" in sections[0]


def test_singular_district_cancellation_has_no_trailing_cancelled_status():
    from app.formatter import bom_notice_sections
    alert = Alert("single-cancel", "Severe Thunderstorm Warning", "", "NSW", "", "", "Alert",
                  specific_locations="Orange", warning_summary="Damaging winds. Severe thunderstorms are no longer occurring in the South West Slopes district and the warning for this district is CANCELLED")
    parts = frame_notice("BOM","UPDATE",alert.event,bom_notice_sections(alert,"Australia/Sydney","UPDATE"),"check bom.gov.au",126,alert.alert_id)
    assert "CANCELLED —" in parts[0]
    assert " ".join(parts).count("CANCELLED") == 1
    assert any("South West Slopes" in p for p in parts)
    assert all(len(p.encode())<=126 for p in parts)
