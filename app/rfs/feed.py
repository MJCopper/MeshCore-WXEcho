"""Official NSW RFS current-incidents GeoJSON feed and field normalization."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from hashlib import sha256

import httpx

from ..config import BOM_USER_AGENT

FEED_URL = "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json"
LEVELS = ("Emergency Warning", "Watch and Act", "Advice")


class RFSFeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class Incident:
    incident_id: str
    name: str
    level: str
    council: str
    location: str
    status: str
    kind: str
    updated: str
    source_url: str
    size: str = ""
    agency: str = ""
    published_raw: str = ""

    @property
    def revision(self) -> str:
        # Feed publication time can change without a meaningful incident change.
        fields = (self.name, self.level, self.council, self.location, self.status, self.kind, self.size, self.agency)
        return sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def council_key(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    value = re.sub(r"^(?:the )?council of (?:the )?", "", value)
    value = re.sub(r"^(?:city|municipality) of (?:the )?", "", value)
    value = re.sub(r"(?:\s+(?:city|regional|shire|municipal|council))+$", "", value).strip()
    # Provider spelling differs from the Spatial Services LGA name.
    return {"midcoast": "mid coast"}.get(value, value)


def _fields(description: str) -> dict[str, str]:
    result = {}
    for part in re.split(r"<br\s*/?>", description or "", flags=re.I):
        cleaned = html.unescape(re.sub(r"<[^>]*>", "", part)).strip()
        if ":" in cleaned:
            key, value = cleaned.split(":", 1)
            result[key.strip().upper()] = " ".join(value.split())
    return result


def parse_incidents(payload: dict) -> list[Incident]:
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise RFSFeedError("RFS returned invalid GeoJSON collection")
    features = payload.get("features")
    if not isinstance(features, list):
        raise RFSFeedError("RFS feed has no feature list")
    incidents = []
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise RFSFeedError("RFS incident is missing valid properties")
        p = feature["properties"]
        fields = _fields(p.get("description", ""))
        guid = str(p.get("guid") or "").strip()
        if not guid:
            raise RFSFeedError("RFS incident is missing its identifier")
        incidents.append(Incident(
            incident_id=guid, name=str(p.get("title") or "").strip(),
            level=str(p.get("category") or fields.get("ALERT LEVEL") or "").strip(),
            council=fields.get("COUNCIL AREA", ""),
            location=fields.get("LOCATION", ""), status=fields.get("STATUS", ""),
            kind=fields.get("TYPE", ""), updated=fields.get("UPDATED", ""),
            source_url=str(p.get("link") or "https://www.rfs.nsw.gov.au/fire-information/fires-near-me"),
            size=fields.get("SIZE", ""), agency=fields.get("RESPONSIBLE AGENCY", ""),
            published_raw=str(p.get("pubDate") or ""),
        ))
    return incidents


class RFSClient:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    async def fetch(self) -> list[Incident]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers={
                "User-Agent": BOM_USER_AGENT, "Accept": "application/geo+json, application/json"
            }) as client:
                response = await client.get(FEED_URL)
                response.raise_for_status()
                return parse_incidents(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise RFSFeedError(f"RFS feed unavailable: {exc}") from exc
