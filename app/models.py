"""Normalized representation of a BOM warning."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class Alert:
    alert_id: str
    event: str
    headline: str
    area_desc: str
    effective: str
    expires: str
    message_type: str  # "Alert", "Update", "Cancel"
    ends: str = ""      # when the HAZARD ends (for "until"); falls back to expires
    onset: str = ""     # when the hazard STARTS (for the upcoming-window display)
    detail: str = ""
    specific_locations: str = ""
    warning_summary: str = ""
    warning_sections: tuple = ()
    references: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_bom(cls, item: dict) -> "Alert":
        return cls(
            alert_id=item.get("id", ""),
            event=(item.get("event") or "").strip(),
            headline=(item.get("headline") or "").strip(),
            area_desc=(item.get("area_desc") or "").strip(),
            effective=item.get("effective") or item.get("onset") or "",
            expires=item.get("expires") or item.get("ends") or "",
            message_type=(item.get("message_type") or "Alert").strip(),
            ends=item.get("ends") or item.get("expires") or "",
            onset=item.get("onset") or item.get("effective") or "",
            detail=(item.get("detail") or "").strip(),
            references=item.get("references", []) or [],
            raw=item.get("raw", item),
        )

    def content_hash(self) -> str:
        """Hash of the fields that determine whether a rebroadcast is warranted."""
        basis = f"{self.event}|{self.headline}|{self.area_desc}|{self.expires}|{self.detail}|{self.warning_sections}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    def revision_hash(self) -> str:
        """Identify a received warning revision, including its issue time."""
        basis = f"{self.message_type}|{self.event}|{self.headline}|{self.area_desc}|{self.effective}|{self.expires}|{self.detail}|{self.warning_sections}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
