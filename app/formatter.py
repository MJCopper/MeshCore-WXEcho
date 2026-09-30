"""Format BOM alerts into MeshCore text payloads (<= 195 bytes).

Multi-area alerts are summarised using the first BOM area followed by
"and surrounding areas". Times are local (tz abbreviation dropped because the
mesh is regional). Upcoming alerts show a start-to-end window; in-effect alerts
show only "until <end>". The payload is byte-capped in UTF-8, area trimmed first.
"""
from __future__ import annotations

from datetime import datetime
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import MAX_PAYLOAD_BYTES

PREFIX = ""


def _to_local(iso: str, tz_name: str):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if tz_name:
        try:
            return dt.astimezone(ZoneInfo(tz_name))
        except (ZoneInfoNotFoundError, ValueError):
            pass  # configured zone unavailable; fall through to local time
    # No zone set (or it could not be resolved): use this machine's local time
    # rather than leaving it in UTC, which reads as hours-off to the operator.
    try:
        return dt.astimezone()
    except Exception:
        return dt


def _clock(dt) -> str:
    try:
        return dt.strftime("%-I:%M %p").strip()
    except ValueError:
        return dt.strftime("%I:%M %p").lstrip("0").strip()


def _now_local(tz_name: str):
    try:
        return datetime.now(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _format_when(onset_iso: str, ends_iso: str, tz_name: str) -> str:
    start = _to_local(onset_iso, tz_name)
    end = _to_local(ends_iso, tz_name)
    now = _now_local(tz_name)
    upcoming = False
    if start is not None and now is not None:
        try:
            upcoming = start > now
        except TypeError:
            upcoming = False
    if upcoming:
        return f"from {_clock(start)} to {_clock(end)}" if end is not None else f"from {_clock(start)}"
    if end is not None:
        return f"until {_clock(end)}"
    return ""


def _area_string(area_desc: str, home_area: str = "") -> str:
    areas = [a.strip() for a in area_desc.split(";") if a.strip()]
    if not areas:
        return ""
    if len(areas) == 1:
        return areas[0]
    primary = next(
        (a for a in areas if home_area and home_area.lower() in a.lower()),
        areas[0],
    )
    return f"{primary} and surrounding areas"


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _truncate_bytes(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _truncate_words_bytes(s: str, max_bytes: int) -> str:
    trimmed = _truncate_bytes(s, max_bytes).rstrip(" ,:;-")
    if _byte_len(trimmed) <= max_bytes:
        return trimmed
    return _truncate_bytes(trimmed, max_bytes).rstrip(" ,:;-")


def _with_part_marker(index: int, total: int, body: str) -> str:
    return f"{PREFIX}{index}/{total} {body}".strip()


def _part_prefix(index: int, total: int) -> str:
    return f"{PREFIX}{index}/{total} "


def format_alert(
    event: str,
    area_desc: str,
    ends_iso: str,
    tz_name: str = "Australia/Sydney",
    onset_iso: str = "",
    home_area: str = "",
    sep: str = "for",
    max_bytes: int = MAX_PAYLOAD_BYTES,
) -> str:
    when = _format_when(onset_iso, ends_iso, tz_name)
    area = _area_string(area_desc, home_area)

    def assemble(area_part: str) -> str:
        body = event
        if area_part:
            body += f" {sep} {area_part}"
        if when:
            body += f" {when}"
        return PREFIX + body

    msg = assemble(area)
    if _byte_len(msg) <= max_bytes:
        return msg
    primary_only = area.replace(" and surrounding areas", "")
    msg = assemble(primary_only)
    if _byte_len(msg) <= max_bytes:
        return msg
    msg = assemble("")
    if _byte_len(msg) <= max_bytes:
        return msg
    return _truncate_bytes(msg, max_bytes)


def _split_complete_message(message: str, max_bytes: int) -> list[str]:
    """Split on words without silently dropping warning areas."""
    if _byte_len(message) <= max_bytes:
        return [message]
    budget = max_bytes - 12  # room for a multipart marker
    chunks = []
    current = ""
    for word in message.split():
        candidate = f"{current} {word}" if current else word
        if _byte_len(candidate) <= budget:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = word
        while _byte_len(current) > budget:
            piece = _truncate_bytes(current, budget)
            chunks.append(piece)
            current = current[len(piece):]
    if current:
        chunks.append(current)
    total = len(chunks)
    return [_with_part_marker(i, total, chunk) for i, chunk in enumerate(chunks, 1)]


def build_mesh_text(alert, tz_name: str = "Australia/Sydney",
                    max_bytes: int = MAX_PAYLOAD_BYTES) -> str:
    """Payload for a non-cancel alert, enriched when BOM page data is available."""
    return build_mesh_parts(alert, tz_name, max_bytes=max_bytes)[0]


def build_mesh_parts(alert, tz_name: str = "Australia/Sydney",
                     max_bytes: int = MAX_PAYLOAD_BYTES) -> list[str]:
    """Return byte-capped parts for an alert, preserving marine warning areas."""
    sections = getattr(alert, "warning_sections", ()) or ()
    if sections:
        parts = []
        for section in sections:
            onset = _to_local(section.onset, tz_name)
            day = f"{onset:%A}: " if onset else ""
            if section.phase == "CAN" or section.phenomenon.casefold() == "cancellation":
                prefix = f"{day}Cancellation of {alert.event} for "
            else:
                prefix = f"{day}{section.phenomenon} for "
            areas = [area.strip() for area in re.split(r",\s*|\s+and\s+", section.areas) if area.strip()]
            current = prefix
            for area in areas:
                candidate = current + (", " if current != prefix else "") + area
                if _byte_len(candidate) > max_bytes and current != prefix:
                    parts.append(current)
                    current = prefix + area
                else:
                    current = candidate
                if _byte_len(current) > max_bytes:
                    parts.extend(_split_complete_message(current, max_bytes))
                    current = prefix
            if current != prefix:
                parts.append(current)
        return parts
    locations = getattr(alert, "specific_locations", "") or ""
    summary = getattr(alert, "warning_summary", "") or ""
    if locations and summary:
        when = _format_when(alert.onset, alert.ends, tz_name)
        intro = f"{alert.event} for {locations}"
        if when:
            intro = f"{intro} {when}"
        one_part = f"{PREFIX}{intro}: {summary}"
        if _byte_len(one_part) <= max_bytes:
            return [one_part]

        p1_budget = max(0, max_bytes - _byte_len(_part_prefix(1, 2)))
        p2_budget = max(0, max_bytes - _byte_len(_part_prefix(2, 2)))
        p1_body = _truncate_words_bytes(intro, p1_budget)
        p2_body = _truncate_words_bytes(summary, p2_budget)
        return [_with_part_marker(1, 2, p1_body), _with_part_marker(2, 2, p2_body)]
    if locations:
        return [format_alert(alert.event, locations, alert.ends, tz_name, onset_iso=alert.onset, max_bytes=max_bytes)]
    return [format_alert(alert.event, alert.area_desc, alert.ends, tz_name,
                         onset_iso=alert.onset, sep="for", max_bytes=max_bytes)]


def fmt_local(iso: str, tz_name: str = "Australia/Sydney") -> str:
    """Human-friendly local timestamp for the UI, e.g. "Jul 28, 1:40 AM".
    Falls back to the raw value if it cannot be parsed."""
    dt = _to_local(iso, tz_name)
    if dt is None:
        return iso or ""
    try:
        return dt.strftime("%b %-d, %-I:%M %p")
    except ValueError:
        return dt.strftime("%b %d, %I:%M %p")
