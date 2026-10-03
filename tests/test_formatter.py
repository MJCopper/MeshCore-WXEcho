from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import FINAL_VERIFICATION_MESSAGE, MAX_PAYLOAD_BYTES
from app.formatter import build_mesh_parts, format_alert


def test_bom_warning_formats_in_australian_timezone():
    end = datetime.now(ZoneInfo("Australia/Sydney")) + timedelta(hours=2)
    message = format_alert("Severe Thunderstorm Warning", "Illawarra", end.isoformat(), "Australia/Sydney")
    assert message.startswith("Severe Thunderstorm Warning for Illawarra")
    assert "until" in message
    assert len(message.encode()) <= 195


def test_multiple_districts_are_preserved():
    message = format_alert("Flood Warning", "Illawarra; Hunter; Central Coast", "", "Australia/Sydney", home_area="Hunter")
    assert message == "Flood Warning for Illawarra, Hunter, Central Coast"


def test_enriched_bom_warning_includes_locations_and_stays_within_limit():
    from app.models import Alert
    from app.formatter import build_mesh_text

    alert = Alert(alert_id="id", event="Severe Thunderstorm Warning",
                  headline="warning", area_desc="WA", effective="", expires="",
                  message_type="Alert", specific_locations="Eyre, Rawlinna and Cocklebiddy",
                  warning_summary="damaging winds, large hailstones and heavy rainfall")
    message = build_mesh_text(alert, "Australia/Perth")
    assert "Eyre, Rawlinna and Cocklebiddy" in message
    assert "damaging winds" in message
    assert len(message.encode()) <= 195


def test_build_mesh_parts_returns_single_part_for_ordinary_alert():
    from app.models import Alert

    alert = Alert(
        alert_id="id",
        event="Flood Warning",
        headline="warning",
        area_desc="Illawarra",
        effective="",
        expires="",
        message_type="Alert",
    )
    parts = build_mesh_parts(alert, "Australia/Sydney")
    assert len(parts) == 1
    assert "1/2" not in parts[0]
    assert len(parts[0].encode()) <= 195


def test_build_mesh_parts_splits_enriched_alert_with_markers_and_byte_caps():
    from app.models import Alert

    alert = Alert(
        alert_id="id",
        event="Severe Thunderstorm Warning",
        headline="warning",
        area_desc="WA",
        effective="2026-09-28T08:00:00+00:00",
        expires="2026-09-28T10:00:00+00:00",
        message_type="Alert",
        ends="2026-09-28T10:00:00+00:00",
        onset="2026-09-28T08:00:00+00:00",
        specific_locations="Eyre, Rawlinna, Cocklebiddy and nearby highways",
        warning_summary=(
            "Damaging winds and large hailstones are likely, with heavy rainfall "
            "that may lead to flash flooding in low-lying roads and creek crossings."
        ),
    )
    parts = build_mesh_parts(alert, "Australia/Perth", max_bytes=120)
    assert len(parts) >= 2
    assert all(part.startswith(f"{i}/{len(parts)} ") for i, part in enumerate(parts, 1))
    assert "Eyre" in " ".join(parts)
    reconstructed = " ".join(part.split(" ", 1)[1] for part in parts)
    assert "Damaging winds" in reconstructed
    assert "creek crossings" in " ".join(parts)
    assert all(len(part.encode()) <= 120 for part in parts)


def test_final_verification_payload_is_exact_and_within_byte_cap():
    assert FINAL_VERIFICATION_MESSAGE == (
        "UNOFFICIAL relay. May be incorrect or incomplete. Verify independently. "
        "Never base safety decisions on these notices."
    )
    assert len(FINAL_VERIFICATION_MESSAGE.encode("utf-8")) <= MAX_PAYLOAD_BYTES


def test_marine_warning_sections_keep_cancellations_distinct():
    from app.models import Alert
    from app.bom_enricher import WarningSection

    alert = Alert(
        alert_id="marine", event="Marine Wind Warning", headline="marine",
        area_desc="New South Wales", effective="", expires="", message_type="Alert",
        warning_sections=(
            WarningSection("Strong Wind Warning", "Hunter Coast, Sydney Coast and Illawarra Coast",
                           "REN", "2026-09-30T06:00:00Z"),
            WarningSection("Cancellation", "Batemans Coast and Eden Coast",
                           "CAN", "2026-09-30T06:00:00Z"),
        ),
    )
    parts = build_mesh_parts(alert, "Australia/Sydney")
    assert len(parts) == 2
    assert parts[0].startswith("Wednesday 30 Sep 2026: Strong Wind Warning for Hunter Coast")
    assert "Hunter Coast" in parts[0]
    assert "Illawarra Coast" in parts[0]
    assert parts[1].startswith("Wednesday 30 Sep 2026: Cancellation of Marine Wind Warning for Batemans Coast")
    assert "Batemans Coast" in parts[1]
    assert "Eden Coast" in parts[1]
    assert all(len(part.encode()) <= MAX_PAYLOAD_BYTES for part in parts)


def test_long_marine_section_splits_without_losing_areas():
    from app.models import Alert
    from app.bom_enricher import WarningSection

    areas = "Hunter Coast, Sydney Coast, Illawarra Coast and Batemans Coast"
    alert = Alert(
        alert_id="marine", event="Marine Wind Warning", headline="marine",
        area_desc="New South Wales", effective="", expires="", message_type="Alert",
        warning_sections=(WarningSection("Strong Wind Warning", areas),),
    )
    parts = build_mesh_parts(alert, max_bytes=55)
    assert len(parts) > 1
    assert all(len(part.encode()) <= 55 for part in parts)
    assert "Batemans Coast" in " ".join(parts)
