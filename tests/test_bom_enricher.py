from pathlib import Path

import httpx
import pytest
import respx

from app.bom_enricher import BOMEnrichment, BOMWarningEnricher, parse_warning_api, parse_warning_page
from app.config import BOM_USER_AGENT


FIXTURE = Path(__file__).parent / "fixtures" / "bom_warning_IDW21033.html"
URL = "https://www.bom.gov.au/warning/severe-thunderstorm-warning/IDW21033"
MOCK_URL = "https://api.bom.gov.au/apikey/v1/warnings/warning/IDW21033"


def test_parse_warning_page_extracts_locations_and_summary():
    result = parse_warning_page(FIXTURE.read_text())
    assert result.locations == "Eyre, Rawlinna and Cocklebiddy"
    assert result.summary.startswith("Severe thunderstorms are likely to produce")
    assert "Safety advice" not in result.summary


def test_enricher_rejects_non_bom_urls():
    result = __import__("asyncio").run(BOMWarningEnricher().enrich("https://example.com/warning"))
    assert result == BOMEnrichment()


def test_api_detail_extracts_locations_and_summary():
    result = parse_warning_api({
        "warning": {
            "area_summary": "<p>Goldfields, Eucla and South Interior</p>",
            "phenomena_summary": "<p>for DAMAGING WINDS</p>",
            "info": [{"summary": "<p>Locations which may be affected include Eyre, Rawlinna and Cocklebiddy.</p>"}],
        }
    })
    assert result.locations == "Eyre, Rawlinna and Cocklebiddy"
    assert result.summary.startswith("Locations which may be affected")


def test_api_detail_retains_area_names_and_cap_polygons():
    result = parse_warning_api({"warning": {"info": [{"area": [{
        "area_desc": "Tamworth Regional",
        "polygon": "-31,150 -31,151 -30,151 -30,150",
    }]}]}})
    assert result.area_names == ("Tamworth Regional",)
    assert result.polygons == ("-31,150 -31,151 -30,151 -30,150",)


@pytest.mark.asyncio
async def test_enricher_caches_successful_api_detail():
    with respx.mock() as router:
        route = router.get(MOCK_URL).mock(return_value=httpx.Response(200, json={"warning": {"info": [{"summary": "<p>Locations which may be affected include Eyre, Rawlinna and Cocklebiddy.</p>"}]}}))
        enricher = BOMWarningEnricher()
        first = await enricher.enrich(URL)
        second = await enricher.enrich(URL)
    assert first == second
    assert route.call_count == 1
    assert route.calls.last.request.headers["user-agent"] == BOM_USER_AGENT


@pytest.mark.asyncio
async def test_enricher_falls_back_on_api_failure():
    with respx.mock() as router:
        router.get(MOCK_URL).mock(return_value=httpx.Response(503))
        result = await BOMWarningEnricher().enrich(URL)
    assert result == BOMEnrichment()

MARINE_LEGACY_URL = "http://reg.bom.gov.au/nsw/warnings/marinewind.shtml"
MARINE_API_URL = "https://api.bom.gov.au/apikey/v1/warnings/warning/IDN20400"
MARINE_PAYLOAD = {
    "warning": {
        "id": "IDN20400",
        "info": [
            {"summary": "Strong Wind Warning for Wednesday for Hunter Coast, Sydney Coast and Illawarra Coast"},
            {"is_hazard": "true", "phase": "REN", "phenomena": "Strong Wind Warning",
             "onset_datetime_utc": "2026-09-30T06:00:00Z",
             "area_summary": "Hunter Coast, Sydney Coast and Illawarra Coast"},
            {"is_hazard": "true", "phase": "CAN", "phenomena": "Cancellation",
             "onset_datetime_utc": "2026-09-30T06:00:00Z",
             "area_summary": "Batemans Coast and Eden Coast"},
        ],
    },
}


def test_marine_warning_keeps_active_and_cancelled_areas_separate():
    result = parse_warning_api(MARINE_PAYLOAD)
    assert len(result.sections) == 2
    assert result.sections[0].phenomenon == "Strong Wind Warning"
    assert result.sections[0].areas == "Hunter Coast, Sydney Coast and Illawarra Coast"
    assert result.sections[1].phase == "CAN"
    assert result.sections[1].areas == "Batemans Coast and Eden Coast"


@pytest.mark.asyncio
async def test_legacy_marine_link_resolves_product_id_and_fetches_api():
    with respx.mock() as router:
        page = router.get(MARINE_LEGACY_URL).mock(return_value=httpx.Response(
            200, text='<div class="product"><p class="p-id">IDN20400</p></div>'))
        api = router.get(MARINE_API_URL).mock(return_value=httpx.Response(200, json=MARINE_PAYLOAD))
        result = await BOMWarningEnricher().enrich(MARINE_LEGACY_URL)
    assert page.call_count == 1
    assert api.call_count == 1
    assert len(result.sections) == 2


@pytest.mark.asyncio
async def test_legacy_lookup_falls_back_when_product_page_fails():
    with respx.mock() as router:
        router.get(MARINE_LEGACY_URL).mock(return_value=httpx.Response(503))
        result = await BOMWarningEnricher().enrich(MARINE_LEGACY_URL)
    assert result == BOMEnrichment()
