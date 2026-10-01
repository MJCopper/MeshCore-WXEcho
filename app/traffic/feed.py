"""Public, keyless Live Traffic NSW GeoJSON feeds and normalisation."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import html
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import BOM_USER_AGENT

BASE_URL = "https://data.livetraffic.com/traffic/hazards/"
FEEDS = {
    "incident": "incident.json",
    "roadwork": "roadwork.json",
    "fire": "fire.json",
    "flood": "flood.json",
    "regional": "regional/lga-incidents-open.json",
}
TYPES = ("incident", "roadwork", "fire", "flood", "regional")
BOUNDARIES_URL = "https://portal.data.nsw.gov.au/arcgis/rest/services/Regions_and_Boundaries/MapServer/2/query"
SOURCE_URL = "https://www.livetraffic.com/"


class TrafficFeedError(RuntimeError):
    pass


def clean(value: object) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())


def _ms(value) -> float | None:
    try:
        return float(value) / 1000 if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class TrafficItem:
    item_id: str
    feed: str
    category: str
    title: str
    road: str
    suburb: str
    council: str
    direction: str
    impact: str
    advice: str
    lon: float | None
    lat: float | None
    start: float | None
    end: float | None
    ended: bool
    updated: float | None

    @property
    def revision(self) -> str:
        fields = (self.category, self.title, self.road, self.suburb, self.council,
                  self.direction, self.impact, self.advice, str(self.ended), str(self.start), str(self.end))
        return hashlib.sha256("\x1f".join(fields).encode()).hexdigest()

    def active(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return not self.ended and (self.start is None or self.start <= now) and (self.end is None or self.end > now)


def parse_feed(feed: str, payload: dict) -> list[TrafficItem]:
    if feed not in FEEDS or not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise TrafficFeedError(f"Invalid {feed} traffic feed")
    if not isinstance(payload.get("features"), list):
        raise TrafficFeedError(f"Missing {feed} traffic features")
    published = _ms(payload.get("lastPublished"))
    if published is None or abs(time.time() - published) > 24 * 3600:
        raise TrafficFeedError(f"Stale {feed} traffic feed")
    items = []
    for feature in payload["features"]:
        if not isinstance(feature, dict) or feature.get("id") is None:
            continue
        p = feature.get("properties") or {}
        if not isinstance(p, dict):
            continue
        roads = p.get("roads") or []
        road = roads[0] if roads and isinstance(roads[0], dict) else {}
        periods = p.get("periods") or []
        period = periods[0] if periods and isinstance(periods[0], dict) else {}
        lanes = road.get("impactedLanes") or []
        lane = lanes[0] if isinstance(lanes, list) and lanes and isinstance(lanes[0], dict) else {}
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        try:
            lon, lat = float(coords[0]), float(coords[1])
            if not (140 <= lon <= 160 and -39 <= lat <= -27):
                lon = lat = None
        except (ValueError, TypeError, IndexError):
            lon = lat = None
        title = clean(p.get("displayName") or p.get("mainCategory") or p.get("headline"))
        category = clean(p.get("mainCategory") or feed).upper()
        impact = clean(period.get("roadextent") or lane.get("extent") or
                       lane.get("description") or p.get("adviceA"))
        items.append(TrafficItem(
            item_id=f"{feed}:{feature['id']}", feed=feed, category=category, title=title,
            road=clean(road.get("mainStreet")), suburb=clean(road.get("suburb")),
            council=clean(p.get("OrgName")) if feed == "regional" else "",
            direction=clean(period.get("direction") or lane.get("affectedDirection")), impact=impact,
            advice=clean(p.get("adviceA")), lon=lon, lat=lat,
            start=_ms(p.get("start")), end=_ms(p.get("end")),
            ended=bool(p.get("ended")), updated=_ms(p.get("lastUpdated")),
        ))
    return items


def _inside_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        ax, ay = a[:2]
        bx, by = b[:2]
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def council_at(lon: float, lat: float, polygons: list[dict]) -> str:
    for feature in polygons:
        geometry = feature.get("geometry") or {}
        shapes = [geometry.get("coordinates", [])] if geometry.get("type") == "Polygon" else geometry.get("coordinates", [])
        for rings in shapes:
            if rings and _inside_ring(lon, lat, rings[0]) and not any(_inside_ring(lon, lat, hole) for hole in rings[1:]):
                return clean(feature.get("properties", {}).get("lganame"))
    return ""


def prepare_councils(polygons: list[dict]) -> list[tuple]:
    """Index polygon bounds once so most point checks skip the expensive rings."""
    prepared = []
    for feature in polygons:
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        shapes = [coordinates] if geometry.get("type") == "Polygon" else coordinates
        for rings in shapes:
            if not rings or not rings[0]:
                continue
            xs = [point[0] for point in rings[0]]
            ys = [point[1] for point in rings[0]]
            prepared.append((min(xs), min(ys), max(xs), max(ys),
                             clean(feature.get("properties", {}).get("lganame")), rings))
    return prepared

def council_at_prepared(lon: float, lat: float, prepared: list[tuple]) -> str:
    for left, bottom, right, top, name, rings in prepared:
        if left <= lon <= right and bottom <= lat <= top:
            if _inside_ring(lon, lat, rings[0]) and not any(
                    _inside_ring(lon, lat, hole) for hole in rings[1:]):
                return name
    return ""


class TrafficClient:
    def __init__(self, timeout: float = 30):
        self.timeout = timeout

    async def fetch(self) -> list[TrafficItem]:
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": BOM_USER_AGENT}) as client:
            responses = []
            seen_ids = set()
            for feed, path in FEEDS.items():
                try:
                    response = await client.get(BASE_URL + path)
                    response.raise_for_status()
                    for item in parse_feed(feed, response.json()):
                        raw_id = item.item_id.partition(":")[2]
                        if raw_id not in seen_ids:
                            seen_ids.add(raw_id)
                            responses.append(item)
                except (httpx.HTTPError, ValueError, TrafficFeedError) as exc:
                    raise TrafficFeedError(f"{feed} feed unavailable: {exc}") from exc
            return responses

    async def boundaries(self) -> list[dict]:
        """Dated NSW Spatial Services snapshot; avoids a boundary API outage at poll time."""
        path = Path(__file__).with_name("nsw_lga.geojson.gz")
        def load():
            with gzip.open(path, "rt", encoding="utf-8") as source:
                features = json.load(source).get("features", [])
            if len(features) < 100:
                raise TrafficFeedError("Bundled council boundaries are incomplete")
            return features
        return await asyncio.to_thread(load)
