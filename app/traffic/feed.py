"""Public, keyless Live Traffic NSW GeoJSON feeds and normalisation."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import html
import json
import math
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx

from ..config import BOM_USER_AGENT
from .schedule import ClosurePeriod, window_state

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
        result = float(value) / 1000 if value is not None else None
        return result if result is not None and math.isfinite(result) else None
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
    road_details: str = ""
    hide_end: bool = False
    periods: tuple[ClosurePeriod, ...] = ()
    public_transport: str = ""
    additional_info: str = ""

    @property
    def revision(self) -> str:
        fields = (self.category, self.title, self.road, self.suburb, self.council,
                  self.direction, self.impact, self.advice, self.road_details,
                  str(self.hide_end), str(self.ended), str(self.start), str(self.end),
                  str(self.lon), str(self.lat), self.public_transport, self.additional_info,
                  json.dumps([asdict(p) for p in self.periods], sort_keys=True))
        return hashlib.sha256("\x1f".join(fields).encode()).hexdigest()

    def active(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return not self.ended and (self.start is None or self.start <= now) and (self.end is None or self.end > now)

    def closure_window(self, now: float | None = None):
        if not self.active(now):
            return "inactive", "Notice has ended or is not yet current"
        return window_state(self.periods, time.time() if now is None else now)


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
            raise TrafficFeedError(f"Invalid {feed} traffic item identifier")
        p = feature.get("properties") or {}
        if not isinstance(p, dict):
            raise TrafficFeedError(f"Invalid {feed} traffic item properties")
        if not isinstance(p.get("ended", False), bool):
            raise TrafficFeedError(f"Invalid {feed} ended flag")
        for name in ("start", "end", "lastUpdated"):
            value = p.get(name)
            if value not in (None, "") and _ms(value) is None:
                raise TrafficFeedError(f"Invalid {feed} {name} timestamp")
        roads = p.get("roads") or []
        roads = [r for r in roads if isinstance(r, dict)] if isinstance(roads, list) else []
        road = roads[0] if roads else {}
        periods = p.get("periods") or []
        periods = [r for r in periods if isinstance(r, dict)] if isinstance(periods, list) else []
        geometry = feature.get("geometry") or {}
        coords = (geometry.get("coordinates") or []) if isinstance(geometry, dict) else []
        try:
            lon, lat = float(coords[0]), float(coords[1])
            if not (140 <= lon <= 160 and -39 <= lat <= -27):
                lon = lat = None
        except (ValueError, TypeError, IndexError):
            lon = lat = None
        title = clean(p.get("displayName") or p.get("mainCategory") or p.get("headline"))
        category = clean(p.get("mainCategory") or feed).upper()
        def joined(values):
            return "; ".join(dict.fromkeys(text for value in values if (text := clean(value))))
        impacts = [period.get("roadextent") for period in periods]
        directions = [period.get("direction") for period in periods]
        for r in roads:
            for entry in r.get("impactedLanes") or []:
                if isinstance(entry, dict):
                    impacts.extend((entry.get("extent"), entry.get("description")))
                    directions.append(entry.get("affectedDirection"))
        impact = joined(impacts)
        advice = joined(p.get(key) for key in ("adviceA", "adviceB", "adviceC", "otherAdvice", "diversions"))
        road_details = joined(
            " ".join(filter(None, (clean(r.get("mainStreet")) if index else "",
                                   clean(r.get("locationQualifier")), clean(r.get("crossStreet")),
                                   clean(r.get("secondLocation")),
                                   clean(r.get("suburb")) if index else "")))
            for index, r in enumerate(roads)
            if index or r.get("crossStreet") or r.get("secondLocation"))
        items.append(TrafficItem(
            item_id=f"{feed}:{feature['id']}", feed=feed, category=category, title=title,
            road=clean(road.get("mainStreet")), suburb=clean(road.get("suburb")),
            council=clean(p.get("OrgName")) if feed == "regional" else "",
            direction=joined(directions), impact=impact,
            advice=advice, lon=lon, lat=lat,
            start=_ms(p.get("start")), end=_ms(p.get("end")),
            ended=bool(p.get("ended")), updated=_ms(p.get("lastUpdated")),
            road_details=road_details, hide_end=p.get("hideEndDate") is True,
            periods=tuple(ClosurePeriod(clean(period.get("fromDay")), clean(period.get("toDay")),
                                        clean(period.get("startTime")), clean(period.get("finishTime")),
                                        clean(period.get("timezone") or p.get("timezone")),
                                        clean(period.get("roadextent")), clean(period.get("direction")))
                          for period in periods),
            public_transport=clean(p.get("publicTransport")),
            additional_info=joined(
                entry.get("value") or entry.get("description") or entry.get("text")
                if isinstance(entry, dict) else entry
                for entry in (p.get("additionalInfo") or [])
            ) if isinstance(p.get("additionalInfo") or [], list) else clean(p.get("additionalInfo")),
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
        self.last_errors: dict[str, str] = {}
        self.last_successful_feeds: set[str] = set()
        self.last_published: dict[str, float] = {}

    async def fetch(self, feeds: set[str] | None = None) -> list[TrafficItem]:
        feeds = set(FEEDS) if feeds is None else set(feeds) & set(FEEDS)
        self.last_errors = {}
        self.last_successful_feeds = set()
        self.last_published = {}
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": BOM_USER_AGENT}) as client:
            responses = []
            for feed, path in FEEDS.items():
                if feed not in feeds:
                    continue
                try:
                    response = await client.get(BASE_URL + path)
                    response.raise_for_status()
                    payload = response.json()
                    responses.extend(parse_feed(feed, payload))
                    self.last_successful_feeds.add(feed)
                    self.last_published[feed] = _ms(payload.get("lastPublished"))
                except (httpx.HTTPError, ValueError, TrafficFeedError) as exc:
                    self.last_errors[feed] = str(exc)
            if feeds and not self.last_successful_feeds:
                raise TrafficFeedError("All selected traffic feeds failed: " + "; ".join(
                    f"{feed}: {error}" for feed, error in self.last_errors.items()))
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


@dataclass(frozen=True)
class TrafficCouncilMatch:
    council: str = ""
    method: str = "unknown"
    reason: str = ""


def match_traffic_council(item: TrafficItem, prepared: list[tuple]) -> TrafficCouncilMatch:
    """Point matching is approximate and cannot establish an entire road footprint."""
    from ..rfs.councils import COUNCILS
    from ..rfs.feed import council_key
    canonical = {council_key(name): name for name in COUNCILS}
    if item.lon is not None and item.lat is not None:
        names = set()
        near_border = False
        x, y = item.lon, item.lat
        tolerance = .001  # roughly 100 m; snapshot coordinates are simplified
        for left, bottom, right, top, name, rings in prepared:
            if not (left - tolerance <= x <= right + tolerance and bottom - tolerance <= y <= top + tolerance):
                continue
            for ring in rings:
                for a, b in zip(ring, ring[1:] + ring[:1]):
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    length = dx * dx + dy * dy
                    fraction = max(0., min(1., ((x-a[0])*dx + (y-a[1])*dy) / length)) if length else 0.
                    distance = (x-a[0]-fraction*dx)**2 + (y-a[1]-fraction*dy)**2
                    if distance <= tolerance * tolerance:
                        near_border = True
            if _inside_ring(x, y, rings[0]) and not any(_inside_ring(x, y, hole) for hole in rings[1:]):
                names.add(canonical.get(council_key(name), name))
        if len(names) == 1 and not near_border:
            return TrafficCouncilMatch(names.pop(), "approximate point",
                                       "Feed point within simplified council boundary; entire road footprint is not established")
        point_reason = "Point is near a simplified council border" if near_border else "Point is outside or ambiguous in the council snapshot"
    else:
        point_reason = "Feed coordinates are missing or invalid"
    supplied = canonical.get(council_key(item.council)) if item.council else None
    if supplied:
        return TrafficCouncilMatch(supplied, "provider council fallback",
                                   point_reason + "; using the provider's named council, not a proven road footprint")
    return TrafficCouncilMatch(reason=point_reason + "; no recognised provider council fallback")
