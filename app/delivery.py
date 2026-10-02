"""Shared notice admission and part-outcome recording for service pollers."""
from __future__ import annotations

import json

from .config import BURST_GAP_SECONDS, MULTIPART_GAP_SECONDS, QUEUE_MAX


def submit_notice(tx, parts: list[str], on_result, priority: int = 3) -> bool:
    """Queue an entire notice in order. A full live queue defers every part."""
    entries = [(part, MULTIPART_GAP_SECONDS if index < len(parts) - 1 else BURST_GAP_SECONDS)
               for index, part in enumerate(parts)]
    batch = getattr(tx, "enqueue_notice", None)
    if batch is not None:
        return batch(entries, on_result=on_result, priority=priority)
    # Older lightweight transmitter doubles do not expose batch admission.
    if len(parts) > QUEUE_MAX or getattr(tx, "queue_depth", 0) + len(parts) > QUEUE_MAX:
        return False
    for index, (part, delay) in enumerate(entries):
        if not tx.enqueue(part, on_result=lambda ok, err="", i=index: on_result(i, ok, err),
                          delay_after=delay):
            return False
    return True


def record_part(db, row_id: int, index: int, total: int, ok: bool, error: str = "") -> None:
    recorder = getattr(db, "record_delivery_part", None)
    if recorder is not None:
        recorder(row_id, index, total, ok, error)


def remaining_parts(row, text: str, parts: list[str]) -> list[int]:
    """Skip parts already confirmed locally on a retry of identical text."""
    if row is None or row["transmitted_text"] != text:
        return list(range(len(parts)))
    try:
        raw = row["delivery_parts"]
        outcomes = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (KeyError, IndexError, TypeError, ValueError):
        outcomes = []
    if len(outcomes) != len(parts):
        return list(range(len(parts)))
    return [index for index, outcome in enumerate(outcomes)
            if outcome.get("status") != "transmitted"]


def queue_refusal(parts: list[str]) -> tuple[str, str]:
    if len(parts) > QUEUE_MAX:
        return "failed", f"Notice has {len(parts)} parts; queue limit is {QUEUE_MAX}"
    return "deferred", "Waiting for space in the MeshCore send queue"


def permanently_unsendable(row, revision: str, text: str) -> bool:
    """Do not re-log an oversized unchanged notice at every poll."""
    return bool(row is not None and row["revision_hash"] == revision
                and row["transmitted_text"] == text
                and row["transmit_status"] == "failed"
                and row["detail"].startswith("Notice has "))
