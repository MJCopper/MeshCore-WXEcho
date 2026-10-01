"""Registry and common record shape for service history.

New sources register a stable ID and display label, then write through
Database.add_service_history. The history page does not know source schemas.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HistorySource:
    source_id: str
    label: str
    facet_label: str = ""
    facet_key: str = ""
    facet_options: tuple[str, ...] = ()


_SOURCES: dict[str, HistorySource] = {}


def register_history_source(source_id: str, label: str,
                            facet_label: str = "", facet_key: str = "",
                            facet_options: tuple[str, ...] = ()) -> None:
    if not source_id or not source_id.replace("_", "").isalnum():
        raise ValueError("history source ID must be alphanumeric")
    _SOURCES[source_id] = HistorySource(source_id, label, facet_label, facet_key, facet_options)


def history_sources() -> tuple[HistorySource, ...]:
    return tuple(_SOURCES.values())


def history_source_label(source_id: str) -> str:
    source = _SOURCES.get(source_id)
    return source.label if source else source_id


register_history_source("bom", "BOM")
register_history_source("rfs", "NSW RFS", "Alert level", "level",
                        ("Emergency Warning", "Watch and Act", "Advice"))


def get_history_source(source_id: str) -> HistorySource | None:
    return _SOURCES.get(source_id)

register_history_source("traffic", "Live Traffic NSW", "Feed", "feed",
                        ("incident", "roadwork", "fire", "flood", "regional"))
