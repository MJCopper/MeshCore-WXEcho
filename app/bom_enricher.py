"""Optional enrichment for linked Bureau of Meteorology warning pages."""
from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from html import unescape
from urllib.parse import urlparse

import httpx

from .config import BOM_USER_AGENT



@dataclass(frozen=True)
class BOMEnrichment:
    locations: str = ""
    summary: str = ""
    sections: tuple["WarningSection", ...] = ()
    area_names: tuple[str, ...] = ()
    polygons: tuple[str, ...] = ()
    issued: str = ""
    expires: str = ""
    lga_names: tuple[str, ...] = ()
    status: str = ""
    geocodes: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class WarningSection:
    phenomenon: str
    areas: str
    phase: str = ""
    onset: str = ""


class _WarningPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paragraphs: list[tuple[str, str]] = []
        self._tag = ""
        self._heading = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"h1", "h2", "h3", "p"}:
            self._tag = tag
            self._parts = []

    def handle_data(self, data):
        if self._tag:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag != self._tag:
            return
        text = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
        if text and tag.startswith("h"):
            self._heading = text
        elif text and tag == "p":
            self.paragraphs.append((self._heading, text))
        self._tag = ""
        self._parts = []


def _clean_sentence(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value


def parse_warning_page(raw: str) -> BOMEnrichment:
    parser = _WarningPageParser()
    parser.feed(raw)
    locations = ""
    summaries: list[tuple[int, str]] = []
    for heading, paragraph in parser.paragraphs:
        if heading.casefold().startswith("safety advice"):
            continue
        match = re.search(r"locations which may be affected include (.+?)(?:\.|$)", paragraph, re.IGNORECASE)
        if match and not locations:
            locations = _clean_sentence(match.group(1))
        lower = paragraph.lower()
        if "likely to produce" in lower:
            summaries.append((3, paragraph))
        elif "thunderstorms are expected" in lower:
            summaries.append((2, paragraph))
        elif lower.startswith("weather situation:"):
            summaries.append((1, paragraph))
    summary = _clean_sentence(max(summaries, key=lambda item: item[0])[1]) if summaries else ""
    return BOMEnrichment(locations=locations, summary=summary)


def _strip_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(value)).strip()


def parse_warning_api(payload: dict) -> BOMEnrichment:
    if not isinstance(payload, dict):
        raise ValueError("BOM warning API did not return an object")
    warning = payload.get("warning", {})
    if warning is None:
        warning = {}
    if not isinstance(warning, dict):
        raise ValueError("BOM warning API returned an invalid warning")
    info = warning.get("info", []) or []
    if not isinstance(info, list) or any(not isinstance(item, dict) for item in info):
        raise ValueError("BOM warning API returned invalid warning information")
    summaries = []
    locations = ""
    geocodes = []
    area_names = []
    polygons = []
    lga_names = []
    untyped_footprint = False
    geographic_codes = []
    for item in info:
        summary = _strip_markup(item.get("summary", ""))
        if summary:
            summaries.append(summary)
            match = re.search(
                r"locations which may be affected include (.+?)(?:\.|$)",
                summary, re.IGNORECASE)
            if match and not locations:
                locations = _clean_sentence(match.group(1))
        for area in item.get("area", []) or []:
            if not isinstance(area, dict):
                raise ValueError("Invalid BOM warning area")
            area_name = _strip_markup(area.get("area_desc", ""))
            if area_name and area_name not in area_names:
                area_names.append(area_name)
            for code in area.get("geocode", []) or []:
                if not isinstance(code, dict):
                    raise ValueError("Invalid BOM warning geocode")
                name = (code.get("name") or "").strip()
                code_type = str(code.get("type") or "").casefold()
                geographic_code = (code_type, str(code.get("code") or ""), name)
                if geographic_code not in geographic_codes:
                    geographic_codes.append(geographic_code)
                if name and code_type in {"lga", "aac:lga", "abs:lga", "local government area", "local-government-area"}:
                    if name not in lga_names:
                        lga_names.append(name)
                elif name and code_type not in {"aac:region", "region", "state"}:
                    untyped_footprint = True
                if name and name not in geocodes:
                    geocodes.append(name)
            raw_polygons = area.get("polygon", []) or []
            if isinstance(raw_polygons, str):
                raw_polygons = [raw_polygons]
            for polygon in raw_polygons:
                if isinstance(polygon, str) and polygon not in polygons:
                    polygons.append(polygon)
            if not area.get("geocode") and area_name:
                untyped_footprint = True
    if not locations:
        locations = _clean_sentence(
            _strip_markup(warning.get("area_summary", "")))
    if not locations and geocodes:
        locations = _clean_sentence(", ".join(geocodes))
    candidates = summaries
    if not candidates:
        candidates = [_strip_markup(warning.get("phenomena_summary", ""))]
    summary = _clean_sentence("; ".join(dict.fromkeys(candidates))) if candidates else ""
    sections = []
    for item in info:
        if str(item.get("is_hazard", "")).lower() != "true":
            continue
        areas = _strip_markup(item.get("area_summary", ""))
        phenomenon = _strip_markup(item.get("phenomena", ""))
        if not areas or not phenomenon:
            continue
        sections.append(WarningSection(
            phenomenon=phenomenon, areas=areas,
            phase=str(item.get("phase") or ""),
            onset=str(item.get("onset_datetime_utc") or ""),
        ))
    meta = payload.get("meta") or {}
    return BOMEnrichment(locations=locations, summary=summary, sections=tuple(sections),
                         area_names=tuple(area_names or geocodes), polygons=tuple(polygons),
                         issued=str(meta.get("issue_datetime_utc") or "") if isinstance(meta, dict) else "",
                         expires=str(warning.get("expires_datetime_utc") or ""),
                         lga_names=tuple(lga_names) if not untyped_footprint else (),
                         status="available" if warning else "unavailable",
                         geocodes=tuple(geographic_codes))


def _warning_api_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if host not in {"www.bom.gov.au", "reg.bom.gov.au"}:
        return ""
    match = re.search(r"/(ID[A-Z0-9]+)(?:\.shtml)?$", parsed.path, re.IGNORECASE)
    if not match:
        return ""
    return "https://api.bom.gov.au/apikey/v1/warnings/warning/%s" % match.group(1).upper()


class BOMWarningEnricher:
    def __init__(self, timeout: float = 7.0, cache_ttl: float = 60.0, max_cache: int = 20):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_cache = max_cache
        self._cache: OrderedDict[str, tuple[float, BOMEnrichment]] = OrderedDict()

    async def enrich(self, url: str) -> BOMEnrichment:
        api_url = _warning_api_url(url)
        if not api_url:
            parsed = urlparse(url)
            if parsed.hostname not in {"www.bom.gov.au", "reg.bom.gov.au"}:
                return BOMEnrichment()
            try:
                async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": BOM_USER_AGENT}) as client:
                    page = await asyncio.wait_for(client.get(url), timeout=self.timeout)
                    page.raise_for_status()
                match = re.search(r'<p\b[^>]*class=["\']p-id["\'][^>]*>\s*(ID[A-Z0-9]+)\s*</p>', page.text, re.IGNORECASE)
                if not match:
                    return BOMEnrichment()
                api_url = "https://api.bom.gov.au/apikey/v1/warnings/warning/%s" % match.group(1).upper()
            except (asyncio.TimeoutError, httpx.HTTPError):
                return BOMEnrichment()
        now = time.monotonic()
        cached = self._cache.get(api_url)
        if cached and cached[0] > now:
            self._cache.move_to_end(api_url)
            return cached[1]
        headers = {
            "User-Agent": BOM_USER_AGENT,
            "Accept": "application/json",
        }
        result = BOMEnrichment()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
                response = await asyncio.wait_for(client.get(api_url), timeout=self.timeout)
                response.raise_for_status()
            result = parse_warning_api(response.json())
        except (asyncio.TimeoutError, httpx.HTTPError, ValueError, TypeError):
            result = BOMEnrichment()
        if result != BOMEnrichment():
            self._cache[api_url] = (now + self.cache_ttl, result)
            self._cache.move_to_end(api_url)
            while len(self._cache) > self.max_cache:
                self._cache.popitem(last=False)
        return result
