"""Format BOM alerts into MeshCore channel text parts.

Broadcast content preserves provider locations and details across byte-capped
parts. Times are rendered in the configured local timezone with their dates.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
import hashlib
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
    if dt.tzinfo is None:
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
        return dt.strftime("%A %-d %b %Y %-I:%M %p").strip()
    except ValueError:
        return dt.strftime("%A %d %b %Y %I:%M %p").strip()


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
    return ", ".join(areas)


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

    return assemble(area)


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


def append_source_note(parts: list[str], note: str, max_bytes: int) -> list[str]:
    """Keep a source note whole, moving it to a new part when necessary."""
    if not parts or _byte_len(note) > max_bytes:
        raise ValueError("MeshCore message budget too small for source note")
    if _byte_len(parts[-1] + note) <= max_bytes:
        return [*parts[:-1], parts[-1] + note]
    return [*parts, note.lstrip("; ")]


def _split_plain(message: str, budget: int) -> list[str]:
    """Wrap at clause boundaries first, then words, without dropping text."""
    if budget < 1:
        raise ValueError("MeshCore message budget too small for notice content")
    chunks = []
    current = ""
    clauses = re.split(r"(?<=[;,])\s+", " ".join(message.split()))
    for clause in clauses:
        candidate = f"{current} {clause}" if current else clause
        if _byte_len(candidate) <= budget:
            current = candidate
            continue
        if _byte_len(clause) <= budget:
            if current:
                chunks.append(current)
            current = clause
            continue
        for word in re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+[,:;.]*|\S+", clause):
            candidate = f"{current} {word}" if current else word
            if _byte_len(candidate) <= budget:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = word
            while _byte_len(current) > budget:
                piece = _truncate_bytes(current, budget)
                if not piece:
                    raise ValueError("MeshCore message budget too small for UTF-8 content")
                chunks.append(piece)
                current = current[len(piece):]
    if current:
        chunks.append(current)
    return chunks


def compact_topic(topic: str, source: str, action: str, max_bytes: int,
                  source_note: str) -> tuple[str, bool]:
    """Reserve room for context, content and the complete source note."""
    prefix = f"{source} {action} #FFFF 99/99 "
    note = source_note.strip(" ;")
    limit = min(max_bytes - _byte_len(prefix + ": " + note),
                max_bytes - _byte_len(prefix + ": ") - 40)
    if limit < 12:
        raise ValueError("MeshCore message budget too small for contextual notice")
    if _byte_len(topic) <= limit:
        return topic, False
    clipped = _truncate_words_bytes(topic, limit - 3)
    return clipped + "...", True


def frame_notice(source: str, action: str, topic: str, sections: list,
                 source_note: str, max_bytes: int, notice_id: str, protected_phrases=()) -> list[str]:
    """Number parts first; identify the ordered notice only in its first part.

    Preserve labels when marine sections change action or hazard type.
    """
    protected = sorted(set(protected_phrases), key=len, reverse=True)
    atoms = [re.escape(p) + r"[,:;.]*" for p in protected if p]
    token_pattern = "|".join(atoms + [r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+[,:;.]*", r"\S+"])
    clean = []
    for section in sections:
        section_action, section_topic, content = (section if isinstance(section, tuple)
                                                  else (action, topic, section))
        content = re.sub(r"^\d+/\d+\s+", "", content or "").strip()
        if content:
            clean.append((section_action, section_topic, content))
    if not clean:
        raise ValueError("notice has no content")
    first_action, first_topic, _ = clean[0]
    bodies = []
    previous = (first_action, first_topic)
    for part_action, part_topic, content in clean:
        if (part_action, part_topic) != previous:
            content = f"{part_action} {part_topic}" + _notice_joiner(content) + content
        bodies.append(content)
        previous = (part_action, part_topic)
    note = source_note.strip(" ;")
    bodies[-1] = bodies[-1].rstrip(" .;") + "; " + note
    reference = hashlib.sha256(notice_id.encode("utf-8")).hexdigest()[:4].upper()
    estimate = 1
    for _ in range(30):
        output = []
        prefixes = []
        for body in bodies:
            body_start = len(output)
            remaining = " ".join(body.split())
            while remaining:
                index = len(output) + 1
                marker = f"{index}/{estimate} " if estimate > 1 else ""
                header = (f"{source} {first_action}" +
                          (f" #{reference}" if estimate > 1 else "") + f" {first_topic}") if index == 1 else ""
                joiner = _notice_joiner(remaining) if header else ""
                prefix = marker + header + joiner
                room = max_bytes - _byte_len(prefix)
                if protected:
                    tokens = re.findall(token_pattern, remaining)
                    chunk = ""
                    for token in tokens:
                        candidate = (chunk + " " + token).strip()
                        if _byte_len(candidate) > room:
                            break
                        chunk = candidate
                    if not chunk and not header:
                        raise ValueError("A required location cannot fit in a MeshCore part")
                else:
                    chunk = _split_plain(remaining, room)[0]
                output.append(prefix + chunk)
                prefixes.append(prefix)
                remaining = remaining[len(chunk):].lstrip()
            # Balance a tiny tail within its own section; never mix cancellation/active sections.
            if len(output) - body_start >= 2:
                previous = output[-2][len(prefixes[-2]):]
                tail = output[-1][len(prefixes[-1]):]
                if _byte_len(tail) < 35:
                    words = re.findall(token_pattern, previous)
                    while len(words) > 1 and _byte_len(tail) < 50:
                        candidate = words[-1] + " " + tail
                        shorter = " ".join(words[:-1])
                        if _byte_len(shorter) < 35 or _byte_len(prefixes[-1] + candidate) > max_bytes:
                            break
                        words.pop()
                        previous, tail = shorter, candidate
                    output[-2] = prefixes[-2] + previous
                    output[-1] = prefixes[-1] + tail
        if len(output) == estimate:
            if any(_byte_len(part) > max_bytes for part in output):
                raise ValueError("MeshCore notice part exceeds byte budget")
            return output
        estimate = len(output)
    raise ValueError("MeshCore notice part numbering did not converge")


def _notice_joiner(content: str) -> str:
    if content.startswith((":", ";", ",")):
        return ""
    if content.startswith("for "):
        return " "
    return ": "


def marine_notice_sections(alert, tz_name: str, action: str = "NEW") -> list[tuple[str, str, str]]:
    """Label each active or cancelled marine section without repeating part headers."""
    out = []
    for section in getattr(alert, "warning_sections", ()) or ():
        onset = _to_local(section.onset, tz_name)
        day = f" {onset:%A} {onset.day} {onset:%b} {onset.year}" if onset else ""
        cancelled = section.phase == "CAN" or section.phenomenon.casefold() == "cancellation"
        section_action = "CANCELLED" if cancelled else action
        section_topic = (alert.event if cancelled else section.phenomenon) + day
        out.append((section_action, section_topic, f"for {section.areas.strip()}"))
    return out


def bom_notice_sections(alert, tz_name: str, action: str):
    """Lead with explicit cancellation sections without cancelling active coverage."""
    if alert.warning_sections:
        sections = marine_notice_sections(alert, tz_name, action)
        if any(part_action == "CANCELLED" for part_action, _, _ in sections):
            cancelled = [(action, alert.event, "CANCELLED — " + topic + " " + content)
                         for part_action, topic, content in sections if part_action == "CANCELLED"]
            active = [(action, alert.event, "ACTIVE WARNING — " + topic + " " + content)
                      for part_action, topic, content in sections if part_action != "CANCELLED"]
            return cancelled + active
        return sections
    summary = alert.warning_summary or " ".join(unescape(re.sub(r"<[^>]*>", " ", alert.detail or "")).split())
    # Extract complete provider cancellation sentences, preserving their scope verbatim.
    sentences = re.split(r"(?<=[.!?])\s+", summary)
    cancellations = [sentence for sentence in sentences if re.search(r"\b(?:the|this) warning(?: for .+?)? (?:is|has been) CANCELLED\b", sentence, re.I)]
    if not cancellations:
        return build_mesh_parts(alert, tz_name, split=False)
    from dataclasses import replace
    active_summary = " ".join(sentence for sentence in sentences if sentence not in cancellations)
    active = replace(alert, warning_summary=active_summary, detail="")
    content = build_mesh_parts(active, tz_name, split=False)[0]
    if content.startswith(alert.event):
        content = content[len(alert.event):].lstrip()
    return [(action, alert.event, "CANCELLED — " + re.sub(r",?\s+and the warning for (?:this|that|these|those) (?:districts?|areas?) is CANCELLED[.!]?$", ".", sentence, flags=re.I)) for sentence in cancellations] + [
        (action, alert.event, "ACTIVE WARNING — " + content)]


def build_mesh_text(alert, tz_name: str = "Australia/Sydney",
                    max_bytes: int = MAX_PAYLOAD_BYTES) -> str:
    """Payload for a non-cancel alert, enriched when BOM page data is available."""
    return build_mesh_parts(alert, tz_name, max_bytes=max_bytes)[0]


def build_mesh_parts(alert, tz_name: str = "Australia/Sydney",
                     max_bytes: int = MAX_PAYLOAD_BYTES, split: bool = True) -> list[str]:
    """Return byte-capped parts for an alert, preserving marine warning areas."""
    sections = getattr(alert, "warning_sections", ()) or ()
    if sections:
        if not split:
            out = []
            for section in sections:
                onset = _to_local(section.onset, tz_name)
                day = f"{onset:%A} {onset.day} {onset:%b} {onset.year}: " if onset else ""
                cancelled = section.phase == "CAN" or section.phenomenon.casefold() == "cancellation"
                label = f"Cancellation of {alert.event}" if cancelled else section.phenomenon
                out.append(f"{day}{label} for {section.areas}")
            return out
        parts = []
        for section in sections:
            onset = _to_local(section.onset, tz_name)
            day = f"{onset:%A} {onset.day} {onset:%b} {onset.year}: " if onset else ""
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
    if not summary:
        summary = " ".join(unescape(re.sub(r"<[^>]*>", " ",
                                         getattr(alert, "detail", "") or "")).split())
    if locations and summary:
        def names(value):
            return {re.sub(r"\s+", " ", name).strip().casefold()
                    for name in re.split(r",\s*|\s+and\s+", value) if name.strip()}
        def remove_repeated(match):
            return "" if names(match.group(1)) == names(locations) else match.group(0)
        summary = re.sub(r"Locations which may be affected include (.+?)(?:\.\s*|$)",
                         remove_repeated, summary, flags=re.I)
        summary = " ".join(summary.split()).strip(" ;")
    if locations and summary:
        when = _format_when(alert.onset, alert.ends, tz_name)
        intro = f"{alert.event} for {locations}"
        if when:
            intro = f"{intro} {when}"
        one_part = f"{PREFIX}{intro}: {summary}"
        if _byte_len(one_part) <= max_bytes:
            return [one_part]

        return _split_complete_message(one_part, max_bytes) if split else [one_part]
    if locations:
        content = format_alert(alert.event, locations, alert.ends, tz_name,
                               onset_iso=alert.onset, max_bytes=max_bytes)
        return _split_complete_message(content, max_bytes) if split else [content]
    body = format_alert(alert.event, alert.area_desc, alert.ends, tz_name,
                        onset_iso=alert.onset, sep="for", max_bytes=max_bytes)
    if summary and summary.casefold() not in body.casefold():
        body += f": {summary}"
    return _split_complete_message(body, max_bytes) if split else [body]


def format_epoch_until(value: float | None, tz_name: str = "Australia/Sydney") -> str:
    """Render a provider epoch end time with an unambiguous local date."""
    if value is None:
        return ""
    try:
        dt = datetime.fromtimestamp(value, timezone.utc).astimezone(ZoneInfo(tz_name))
    except (OverflowError, OSError, TypeError, ValueError, ZoneInfoNotFoundError):
        return ""
    return f"until {_clock(dt)}"


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


def fmt_epoch(value, tz_name: str = "Australia/Sydney") -> str:
    if value is None:
        return ""
    try:
        return fmt_local(datetime.fromtimestamp(value, timezone.utc).isoformat(), tz_name)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
