"""Independent NSW RFS poller, council filter, history and alert queueing."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..config import FINAL_VERIFICATION_MESSAGE, MAX_PAYLOAD_BYTES, VERIFICATION_INTERVAL_SECONDS, polling_seconds
from ..formatter import _split_complete_message, append_source_note
from .feed import LEVELS, RFSClient, RFSFeedError, council_key
from .councils import COUNCILS

logger = logging.getLogger("wx_echo.rfs")


def format_incident(incident, budget: int) -> list[str]:
    place = incident.location or incident.council or incident.name
    body = f"NSW RFS {incident.level}: {incident.name}"
    if place and council_key(place) != council_key(incident.name):
        body += f"; {place}"
    if incident.council:
        body += f"; {incident.council} council"
    if incident.status:
        body += f"; {incident.status}"
    return append_source_note(_split_complete_message(body, budget), "; check rfs.nsw.gov.au", budget)


class RFSPoller:
    def __init__(self, db, tx, client=None):
        self.db, self.tx = db, tx
        self.client = client or RFSClient()
        self.task = None
        self.event = asyncio.Event()
        self.last_poll = ""
        self.last_successful_poll = ""
        self.last_result = "not polled"
        self._poke_generation = 0

    def start(self):
        recovered = self.db.rfs_recover_queued()
        if recovered:
            logger.warning("Recovered %s interrupted RFS sends", recovered)
        self.task = asyncio.create_task(self._run(), name="rfs-poller")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def poke(self):
        self._poke_generation += 1
        self.event.set()

    async def _run(self):
        while True:
            generation = self._poke_generation
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("RFS poll failed")
                self.last_poll = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.last_result = f"error: {exc}"
                self.db.add_error("rfs", str(exc))
            if self._poke_generation != generation:
                continue
            self.event.clear()
            try:
                interval = polling_seconds(self.db.get_setting("rfs_poll_minutes", 10), 10)
                await asyncio.wait_for(self.event.wait(), interval)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self):
        settings = self.db.all_settings()
        if not settings.get("rfs_enabled", False):
            self.last_result = "disabled"
            return
        if not settings.get("rfs_all_councils") and not settings.get("rfs_councils"):
            self.last_result = "select councils or All NSW"
            return
        try:
            incidents = await self.client.fetch()
        except RFSFeedError as exc:
            self.last_poll = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.last_result = f"error: {exc}"
            self.db.add_error("rfs", str(exc))
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.last_poll = now
        self.last_result = f"ok: {len(incidents)} RFS incidents"
        selected = {council_key(x) for x in settings.get("rfs_councils", [])}
        known_nsw = {council_key(x) for x in COUNCILS}
        levels = set(settings.get("rfs_levels", [])) & set(LEVELS)
        budget = getattr(self.tx, "message_budget", MAX_PAYLOAD_BYTES)
        dry_run = bool(settings.get("dry_run", True))
        queued = False
        priority = {"Emergency Warning": 0, "Watch and Act": 1, "Advice": 2}
        for incident in sorted(incidents, key=lambda i: priority.get(i.level, 3)):
            previous = self.db.rfs_get_incident(incident.incident_id)
            self.db.rfs_save_incident(incident, incident.revision)
            council = council_key(incident.council)
            reason = ("alert level" if incident.level not in levels else
                      "council" if council not in (known_nsw if settings.get("rfs_all_councils") else selected) else "")
            latest = self.db.rfs_latest_history(incident.incident_id)
            if reason:
                disposition = "excluded-" + reason.replace(" ", "-")
                if not latest or latest["revision_hash"] != incident.revision or latest["disposition"] != disposition:
                    self.db.rfs_add_history(incident, "", "", f"Excluded by {reason} selection", disposition)
                continue
            if previous and previous["last_sent_hash"] == incident.revision:
                continue
            latest = self.db.rfs_latest_broadcast(incident.incident_id)
            if latest and latest["revision_hash"] == incident.revision and latest["transmit_status"] == "queued":
                continue
            if latest and latest["revision_hash"] == incident.revision and latest["transmit_status"] == "dry-run" and dry_run:
                continue
            parts = format_incident(incident, budget)
            text = " || ".join(parts)
            if dry_run:
                self.db.rfs_add_history(incident, text, "dry-run")
                continue
            row_id = self.db.rfs_add_history(incident, text, "queued")
            result = {"remaining": len(parts), "ok": True, "error": ""}

            def on_result(ok, error="", item=incident, row=row_id, state=result):
                state["remaining"] -= 1
                if not ok:
                    state["ok"] = False
                    state["error"] = state["error"] or error or "send failed"
                if state["remaining"]:
                    return
                if state["ok"]:
                    self.db.rfs_update_history(row, "success")
                    self.db.rfs_mark_sent(item.incident_id, item.revision)
                else:
                    self.db.rfs_update_history(row, "failed", state["error"])
                    self.db.add_error("rfs", f"RFS broadcast failed: {item.name}: {state['error']}")

            for part in parts:
                self.tx.enqueue(part, on_result=on_result, delay_after=3)
            queued = True
        for missing in self.db.rfs_missing_after_poll({item.incident_id for item in incidents}):
            if missing["missing_polls"] != 2:
                continue
            latest = self.db.rfs_latest_history(missing["incident_id"])
            broadcast = self.db.rfs_latest_broadcast(missing["incident_id"])
            if not broadcast or (latest and latest["disposition"] == "absent-from-feed"):
                continue
            self.db.add_service_history(
                "rfs", missing["incident_id"], missing["name"], missing["council"],
                disposition="absent-from-feed",
                detail="Absent from two successful feed polls; resolution not confirmed",
                revision_hash=missing["revision_hash"],
                metadata={"level": missing["level"], "status": missing["status"]},
            )
        if queued:
            self._queue_verification(dry_run)
        self.last_successful_poll = self.last_poll

    def _queue_verification(self, dry_run: bool):
        budget = getattr(self.tx, "message_budget", MAX_PAYLOAD_BYTES)
        if len(FINAL_VERIFICATION_MESSAGE.encode()) > budget:
            self.db.add_error("rfs", f"verification message exceeds MeshCore limit ({budget} bytes)")
            return
        if dry_run:
            return
        key = "verification_live_last_ts"
        saved = self.db.get_setting(key, {}) or {}
        channel = str(self.db.get_setting("meshcore_channel", 0))
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(saved.get(channel, ""))).total_seconds()
        except (ValueError, TypeError):
            elapsed = VERIFICATION_INTERVAL_SECONDS

        def on_result(ok, error=""):
            if ok:
                current = self.db.get_setting(key, {}) or {}
                current[channel] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.db.set_setting(key, current)
            else:
                self.db.add_error("rfs", f"verification message failed: {error}")

        self.tx.enqueue_verification(FINAL_VERIFICATION_MESSAGE, on_result=on_result,
                                     allow_new=elapsed >= VERIFICATION_INTERVAL_SECONDS)
