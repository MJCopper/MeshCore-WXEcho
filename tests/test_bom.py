from pathlib import Path

import pytest

from app.bom import BOM_BASE_URL, BOMError, parse_rss
from app.models import Alert


FIXTURE = Path(__file__).parent / "fixtures" / "bom_warnings_nsw.xml"


def test_bom_uses_canonical_feed_host():
    assert BOM_BASE_URL == "https://reg.bom.gov.au"


def test_parse_bom_rss_items():
    alerts = parse_rss(FIXTURE.read_text(), "https://www.bom.gov.au/fwo/NSW.xml")
    assert len(alerts) == 2
    assert alerts[0]["id"].endswith("IDN21001")
    assert alerts[0]["event"] == "Severe Thunderstorm Warning"
    assert alerts[0]["area_desc"] == "Illawarra"
    assert alerts[0]["references"][0].endswith("IDN21001")


def test_bom_alert_is_normalized():
    item = parse_rss(FIXTURE.read_text())[0]
    alert = Alert.from_bom(item)
    assert alert.alert_id.endswith("IDN21001")
    assert alert.effective.endswith("+00:00")
    assert alert.detail.startswith("Severe thunderstorms")


def test_bom_district_filter_and_cancellation():
    alerts = parse_rss(FIXTURE.read_text(), districts=["Hunter"])
    assert len(alerts) == 1
    assert alerts[0]["message_type"] == "Cancel"
    assert alerts[0]["event"] == "Severe Thunderstorm Warning"


def test_invalid_bom_xml_raises():
    with pytest.raises(BOMError):
        parse_rss("<not-rss>")
