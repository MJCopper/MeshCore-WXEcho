"""Bureau of Meteorology warning RSS client."""
from __future__ import annotations

import asyncio
import re
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx

from .config import BOM_USER_AGENT


class BOMError(RuntimeError):
    pass


BOM_BASE_URL = "https://reg.bom.gov.au"
BOM_FEEDS = {
    "NSW": "/fwo/IDZ00054.warnings_nsw.xml",
}


def _text(element, name: str) -> str:
    child = element.find(name)
    return (child.text or "").strip() if child is not None else ""


def _parse_date(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, IndexError):
        return value


def _split_title(title: str) -> tuple[str, str]:
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"^\d{1,2}/\d{1,2}:\d{2}\s+[A-Z]{2,5}\s+", "", title)
    for marker in (" for ", " - ", ": "):
        if marker in title:
            event, area = title.split(marker, 1)
            return re.sub(r"\s+Summary$", "", event).strip(), area.strip()
    return re.sub(r"\s+Summary$", "", title).strip(), ""


def parse_rss(raw: str, source_url: str = "", districts: list[str] | None = None) -> list[dict]:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise BOMError("BOM returned invalid RSS XML") from exc
    if root.tag != "rss" or root.find("channel") is None:
        raise BOMError("BOM returned a document without an RSS channel")

    alerts = []
    for item in root.findall("./channel/item"):
        title = _text(item, "title")
        raw_link = _text(item, "link")
        link = urljoin(source_url or BOM_BASE_URL, raw_link) if raw_link else ""
        guid = _text(item, "guid") or link
        if not title or not guid:
            raise BOMError("BOM RSS item is missing its title or identifier")
        event, area = _split_title(title)
        message_type = "Cancel" if event.lower().startswith("cancellation of ") else "Alert"
        if message_type == "Cancel":
            event = event[len("Cancellation of "):].strip()
        published = _parse_date(_text(item, "pubDate"))
        description = _text(item, "description")
        if districts:
            haystack = " ".join((event, area, title, description)).casefold()
            if not any(d.casefold() in haystack for d in districts):
                continue
        alerts.append({
            "id": guid,
            "event": event,
            "headline": title,
            "area_desc": area,
            "effective": published,
            "expires": "",
            "message_type": message_type,
            "ends": "",
            "onset": "",
            "detail": description,
            "references": [link] if link else [],
            "raw": {"title": title, "link": link, "guid": guid,
                    "pubDate": _text(item, "pubDate"),
                    "description": description},
        })
    return alerts


class BOMClient:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.last_server_date: str | None = None
        self.last_errors: list[str] = []
        self.last_successful_regions: set[str] = set()

    async def fetch_active(self, regions: list[str] | str,
                           districts: list[str] | None = None) -> tuple[list[dict], str]:
        values = [regions] if isinstance(regions, str) else regions
        states = {value.strip().upper() for value in values if value.strip()}
        states = states or set(BOM_FEEDS)
        unknown = states - BOM_FEEDS.keys()
        if unknown:
            raise BOMError("unknown BOM feed region(s): %s" % ", ".join(sorted(unknown)))

        headers = {
            "User-Agent": BOM_USER_AGENT,
            "Accept": "application/rss+xml, application/xml",
            "Accept-Encoding": "gzip, deflate",
        }
        alerts: list[dict] = []
        raw_parts: list[str] = []
        self.last_errors = []
        self.last_successful_regions = set()
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            async def fetch_state(state: str):
                url = urljoin(BOM_BASE_URL, BOM_FEEDS[state])
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    return state, url, None, str(exc)
                return state, url, response, ""

            results = await asyncio.gather(*(fetch_state(state) for state in sorted(states)))
            for state, url, response, error in results:
                if error:
                    self.last_errors.append("%s: %s" % (state, error))
                    continue
                raw = response.text
                try:
                    parsed = parse_rss(raw, url, districts=districts)
                except BOMError as exc:
                    self.last_errors.append("%s: %s" % (state, exc))
                    continue
                raw_parts.append(raw)
                self.last_successful_regions.add(state)
                for alert in parsed:
                    alert["region"] = state
                alerts.extend(parsed)
                self.last_server_date = response.headers.get("date") or self.last_server_date
        if not raw_parts:
            detail = "; ".join(self.last_errors) or "no feed responses"
            raise BOMError("all selected BOM feeds failed: %s" % detail)
        return alerts, "\n\n".join(raw_parts)
