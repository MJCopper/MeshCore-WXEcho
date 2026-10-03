"""Conservative council matching for NSW BOM warnings.

A broad forecast district or place-name list is not a complete council footprint.
Only explicit LGA names or warning polygons can prove a council match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .rfs.councils import COUNCILS
from .rfs.feed import council_key
from .traffic.feed import _inside_ring


@dataclass(frozen=True)
class CouncilMatch:
    status: str  # matched or unknown
    councils: tuple[str, ...] = ()
    method: str = ""
    reason: str = ""


def _explicit_councils(names: tuple[str, ...], typed: bool = False) -> CouncilMatch | None:
    known = {council_key(name): name for name in COUNCILS}
    found = set()
    for text in names:
        if not text:
            continue
        # A partial list cannot prove that another selected council is outside.
        for piece in re.split(r"\s*(?:,|;|\band\b)\s*", text, flags=re.I):
            if not typed and not re.search(r"\b(?:council|shire|municipality|regional|city)\b", piece, re.I):
                return None
            key = council_key(piece.strip())
            if key not in known:
                return None
            found.add(known[key])
    return CouncilMatch("matched", tuple(sorted(found)), "typed LGA names" if typed else "explicit LGA names",
                        "Provider supplied an administrative council designation") if found else None


def _cap_polygon(value) -> list[tuple[float, float]]:
    """CAP polygons use latitude,longitude pairs; return longitude,latitude."""
    if not isinstance(value, str):
        return []
    points = []
    for pair in value.split():
        try:
            lat, lon = (float(part) for part in pair.split(",", 1))
        except (TypeError, ValueError):
            return []
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return []
        points.append((lon, lat))
    return points if len(points) >= 3 else []


def _segments_cross(a, b, c, d) -> bool:
    def turn(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    ab1, ab2 = turn(a, b, c), turn(a, b, d)
    cd1, cd2 = turn(c, d, a), turn(c, d, b)
    return ab1 * ab2 < 0 and cd1 * cd2 < 0


def _overlaps(warning: list[tuple[float, float]], rings: list) -> bool:
    outer = rings[0]
    if any(_inside_ring(x, y, outer) and not any(
            _inside_ring(x, y, hole) for hole in rings[1:])
            for x, y in warning):
        return True
    if any(_inside_ring(x, y, warning) for x, y in outer):
        return True
    for a, b in zip(warning, warning[1:] + warning[:1]):
        for c, d in zip(outer, outer[1:] + outer[:1]):
            if _segments_cross(a, b, c, d):
                return True
    return False


def match_councils(area: str, area_names: tuple[str, ...], polygons: tuple[str, ...],
                   prepared: list[tuple] | None = None, typed_lgas: tuple[str, ...] = ()) -> CouncilMatch:
    if polygons and prepared:
        matches = set()
        canonical = {council_key(name): name for name in COUNCILS}
        valid = 0
        for raw in polygons:
            ring = _cap_polygon(raw)
            if not ring:
                continue
            valid += 1
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            left, right, bottom, top = min(xs), max(xs), min(ys), max(ys)
            for c_left, c_bottom, c_right, c_top, name, rings in prepared:
                if c_left <= right and c_right >= left and c_bottom <= top and c_top >= bottom:
                    if _overlaps(ring, rings):
                        matches.add(canonical.get(council_key(name), name))
        if valid == len(polygons) and matches:
            return CouncilMatch("matched", tuple(sorted(matches)), "polygon",
                                "Warning polygon intersects simplified council boundaries; borders are approximate")
        return CouncilMatch("unknown", reason="Warning polygons are incomplete, invalid or outside the council snapshot")
    if polygons:
        return CouncilMatch("unknown", reason="Council boundaries unavailable for warning polygon matching")
    if typed_lgas:
        return _explicit_councils(typed_lgas, typed=True) or CouncilMatch("unknown", reason="Unrecognised typed LGA names")
    return _explicit_councils(area_names or (area,)) or CouncilMatch(
        "unknown", reason="Place, forecast district or coast names do not establish a council footprint")
