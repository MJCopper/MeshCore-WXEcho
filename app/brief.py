"""Brief radio notices; provider detail stays in the database and web pages."""
from __future__ import annotations

import re
from html import unescape

from .formatter import frame_notice, _to_local, _area_string


class NoticeTooLong(ValueError):
    """Required notice content cannot meet the radio part policy."""


def compact_time(value, tz_name):
    dt = _to_local(value, tz_name)
    return f"{dt.day} {dt:%b %H:%M}" if dt else ""


def compact_text(value):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value or "")).split()).strip(" ;.")


def unique(values):
    result = []
    for value in values:
        value = compact_text(value)
        if value and value.casefold() not in {v.casefold() for v in result}:
            result.append(value)
    return result


def brief_parts(source, action, topic, sections, optional, note, budget, notice_id):
    """Sections are (required context, full locations); optional detail never adds parts.

    A third part is permitted only when removing locations makes the required
    message fit in two. This counterfactual includes headers, numbering and notes.
    """
    # Keep short addresses together, otherwise protect individual named places.
    short_addresses = [
        loc for _, loc in sections
        if loc and len(loc.encode()) <= 80 and len(loc.split(",")) <= 2
        and ";" not in loc and " and " not in loc
    ]
    places = [name for _, loc in sections
              for name in re.split(r",\s*|;\s*|\s+and\s+", loc) if name.strip()]
    short_clauses = [clause for detail in optional
                     for clause in re.split(r";\s*", detail or "")
                     if len(clause.encode()) <= budget - 4]
    protected = unique(short_addresses + places + short_clauses + [note])


    def render(include_locations=True, extras=()):
        bodies = []
        for core, locations in sections:
            core, locations = compact_text(core), compact_text(locations)
            if include_locations and locations:
                core = (core + " for " if core else "for ") + locations
            if core:
                bodies.append(core)
        body = "; ".join(bodies + list(extras)) or "."
        if len(body.encode()) > budget * 3:
            raise NoticeTooLong(
                f"Formatting blocked: required {source} content exceeds 3 parts; "
                "no locations omitted. Full notice remains available.")
        try:
            return frame_notice(source, action, topic, [body], note, budget, notice_id,
                                protected_phrases=protected if include_locations else [note])
        except ValueError as exc:
            raise NoticeTooLong(f"Formatting blocked: {source} required content cannot fit the radio byte budget; no locations omitted.") from exc

    required = render()
    if len(required) > 2:
        if len(required) > 3 or not any(loc for _, loc in sections) or len(render(False)) > 2:
            raise NoticeTooLong(
                f"Formatting blocked: required {source} notice needs {len(required)} parts. "
                "Target is 2; a third is allowed only for affected locations. "
                "No locations were omitted; full notice remains available.")
        return required  # Do not use the location exception to add descriptive detail.
    accepted = []
    for detail in unique(optional):
        if len(detail.encode()) > budget * 2:
            continue
        try:
            candidate = render(extras=accepted + [detail])
        except NoticeTooLong:
            continue
        if len(candidate) <= 2:
            accepted.append(detail)
            required = candidate
    return required


def _location_names(value):
    return unique(re.split(r",\s*|\s+and\s+", value or ""))


def brief_bom_parts(alert, tz_name, action, budget):
    topic = re.sub(r"^Cancellation of\s+", "", alert.event, flags=re.I)
    optional = []
    sections = []
    if alert.warning_sections:
        ordered = sorted(alert.warning_sections,
                         key=lambda s: not (s.phase == "CAN" or s.phenomenon.casefold() == "cancellation"))
        for section in ordered:
            cancelled = section.phase == "CAN" or section.phenomenon.casefold() == "cancellation"
            when = _to_local(section.onset, tz_name)
            day = f"{when.day} {when:%b}" if when else ""
            core = "CANCELLED" if cancelled else re.sub(r"\s+Warning$", "", section.phenomenon, flags=re.I)
            if day:
                core += " " + day
            sections.append((core, section.areas))
    else:
        summary = compact_text(alert.warning_summary or alert.detail)
        locations = _location_names(alert.specific_locations or _area_string(alert.area_desc))
        # Preserve additional provider location lists even when the primary list differs.
        for match in re.finditer(r"Locations which may be affected include (.+?)(?:\.|$)", summary, re.I):
            locations = unique(locations + _location_names(match[1]))
        for match in re.finditer(r"(?:detected near|forecast to affect) (.+?)(?:\.|$)", summary, re.I):
            scope = re.sub(r"\s+by\s+\d{1,2}:\d{2}\s*(?:am|pm)", "", match[1], flags=re.I)
            locations = unique(locations + _location_names(scope))
        cancellations = []
        for sentence in re.split(r"(?<=[.!?])\s+", summary):
            if re.search(r"\b(?:the|this) warning(?: for .+?)? (?:is|has been) CANCELLED\b", sentence, re.I):
                scope = re.search(r"no longer occurring in (.+?)(?:,? and the warning|$)", sentence, re.I)
                if not scope:
                    scope = re.search(r"(?:the|this) warning for (.+?) (?:is|has been) CANCELLED", sentence, re.I)
                if scope:
                    scope_name = re.sub(r"^(?:the)\s+|\s+districts?$", "", scope[1], flags=re.I)
                    cancellations.append(("CANCELLED", scope_name))
                else:
                    cancellations.append(("CANCELLED: " + sentence, ""))
                summary = summary.replace(sentence, "")
        sections.extend(cancellations)
        # Extract named hazards conservatively; retain negated statements verbatim.
        causes = []
        hazard_names = (
            (r"damaging winds?", "damaging winds"),
            (r"destructive winds?", "destructive winds"),
            (r"large hail(?:stones)?", "large hail"),
            (r"giant hail(?:stones)?", "giant hail"),
            (r"(?<!large )(?<!giant )hail(?:stones)?", "hail"),
            (r"heavy rainfall|heavy rain", "heavy rain"),
            (r"flash flooding", "flash flooding"),
            (r"(?<!flash )flooding", "flooding"),
            (r"storm surge", "storm surge"),
            (r"hazardous surf|heavy surf", "hazardous surf"),
            (r"abnormally high tides", "abnormally high tides"),
            (r"tornado(?:es)?", "tornadoes"),
            (r"blizzard", "blizzard"),
            (r"snow", "snow"),
            (r"heatwave", "heatwave"),
        )
        for sentence in re.split(r"(?<=[.!?])\s+", summary):
            matched = [name for pattern, name in hazard_names if re.search(r"\b(?:" + pattern + r")\b", sentence, re.I)]
            if matched:
                if re.search(r"\b(?:not|no|unlikely|without)\b", sentence, re.I):
                    causes.append(sentence)  # Preserve negation rather than invert the warning.
                else:
                    causes.append("Risk: " + ", ".join(matched))
            elif re.search(r"flooding", sentence, re.I):
                causes.append(sentence)
        # Unknown warning causes are retained verbatim, rather than guessed or dropped.
        if summary and not causes:
            first = re.split(r"(?<=[.!?])\s+", summary)[0]
            if not first.casefold().startswith("locations which may be affected"):
                causes.append(first)
        core = "; ".join(unique(causes))
        until = compact_time(alert.ends, tz_name)
        onset = compact_time(alert.onset, tz_name)
        timing = ("from " + onset + " " if onset else "") + ("until " + until if until else "")
        if timing:
            core = "; ".join(x for x in (core, timing) if x)
        if cancellations and action != "CANCELLED":
            core = "ACTIVE" + (": " + core.removeprefix("Risk: ") if core else "")
        if action == "CANCELLED":
            sections = [("", ", ".join(locations))]
        else:
            sections.append((core, ", ".join(locations)))
    return brief_parts("BOM", action, topic, sections, optional,
                       "check bom.gov.au", budget, alert.alert_id)
