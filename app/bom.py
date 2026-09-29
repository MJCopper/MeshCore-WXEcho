"""Bureau of Meteorology warning RSS client."""
from __future__ import annotations

import asyncio
import re
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx


class BOMError(RuntimeError):
    pass


BOM_BASE_URL = "https://reg.bom.gov.au"
BOM_FEEDS = {
    "NSW": "/fwo/IDZ00054.warnings_nsw.xml",
    "VIC": "/fwo/IDZ00059.warnings_vic.xml",
    "QLD": "/fwo/IDZ00056.warnings_qld.xml",
    "WA": "/fwo/IDZ00060.warnings_wa.xml",
    "SA": "/fwo/IDZ00057.warnings_sa.xml",
    "TAS": "/fwo/IDZ00058.warnings_tas.xml",
    "NT": "/fwo/IDZ00055.warnings_nt.xml",
    "ACT": "/fwo/IDZ00085.warnings_act.xml",
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

    alerts = []
    for item in root.findall("./channel/item"):
        title = _text(item, "title")
        link = urljoin(source_url or BOM_BASE_URL, _text(item, "link"))
        guid = _text(item, "guid") or link
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
            "onset": published,
            "detail": description,
            "references": [link] if link else [],
            "raw": {"title": title, "link": link, "guid": guid,
                    "pubDate": _text(item, "pubDate"),
                    "description": description},
        })
    return alerts


class BOMClient:
    def __init__(self, contact: str = "", timeout: float = 30.0):
        self.contact = contact
        self.timeout = timeout
        self.last_server_date: str | None = None
        self.last_errors: list[str] = []

    async def fetch_active(self, regions: list[str] | str,
                           districts: list[str] | None = None) -> tuple[list[dict], str]:
        values = [regions] if isinstance(regions, str) else regions
        states = {value.strip().upper() for value in values if value.strip()}
        states = states or set(BOM_FEEDS)
        unknown = states - BOM_FEEDS.keys()
        if unknown:
            raise BOMError("unknown BOM feed region(s): %s" % ", ".join(sorted(unknown)))

        contact = self.contact or "contact@example.com"
        headers = {
            "User-Agent": f"MeshCore-BOM-Weather/1.0 ({contact})",
            "Accept": "application/rss+xml, application/xml",
            "Accept-Encoding": "gzip, deflate",
        }
        alerts: list[dict] = []
        raw_parts: list[str] = []
        self.last_errors = []
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
                alerts.extend(parsed)
                self.last_server_date = response.headers.get("date") or self.last_server_date
        if not raw_parts:
            detail = "; ".join(self.last_errors) or "no feed responses"
            raise BOMError("all selected BOM feeds failed: %s" % detail)
        return alerts, "\n\n".join(raw_parts)