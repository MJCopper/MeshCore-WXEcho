from pathlib import Path

import httpx
import pytest
import respx

from app.bom_enricher import BOMEnrichment, BOMWarningEnricher, parse_warning_api, parse_warning_page


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


@pytest.mark.asyncio
async def test_enricher_caches_successful_api_detail():
    with respx.mock() as router:
        route = router.get(MOCK_URL).mock(return_value=httpx.Response(200, json={"warning": {"info": [{"summary": "<p>Locations which may be affected include Eyre, Rawlinna and Cocklebiddy.</p>"}]}}))
        enricher = BOMWarningEnricher()
        first = await enricher.enrich(URL)
        second = await enricher.enrich(URL)
    assert first == second
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_enricher_falls_back_on_api_failure():
    with respx.mock() as router:
        router.get(MOCK_URL).mock(return_value=httpx.Response(503))
        result = await BOMWarningEnricher().enrich(URL)
    assert result == BOMEnrichment()
