"""Independent NSW RFS poller, council filter, history and alert queueing."""
from __future__ import annotations

import asyncio
import time
import logging
from datetime import datetime, timedelta, timezone

from ..config import FINAL_VERIFICATION_MESSAGE, MAX_PAYLOAD_BYTES, VERIFICATION_INTERVAL_SECONDS, polling_seconds
from ..brief import brief_parts, NoticeTooLong
from ..formatter import compact_topic
from ..delivery import permanently_unsendable, queue_refusal, record_part, remaining_parts, submit_notice
from .feed import LEVELS, RFSClient, RFSFeedError, council_key
from .councils import COUNCILS

logger = logging.getLogger("wx_echo.rfs")


def format_incident(incident, budget: int, action: str = "NEW") -> list[str]:
    # Preserve the warning level, cause, status and fullest location once.
    topic, _ = compact_topic(incident.level or "Incident", "NSW RFS", action,
                             budget, "check rfs.nsw.gov.au")
    name, location = incident.name.strip(), incident.location.strip()
    name_key, location_key = council_key(name), council_key(location)
    if name_key and name_key in location_key:
        details = [location]
    elif location_key and location_key in name_key:
        details = [name]
    else:
        details = [value for value in (name, location) if value]
    kind = incident.kind if incident.kind.casefold() not in incident.name.casefold() else ""
    core = "; ".join(value for value in (kind, incident.status) if value)
    optional = [f"{incident.council} council" if incident.council else "",
                "Reported size: " + incident.size if incident.size else "",
                "Agency: " + incident.agency if incident.agency else ""]
    return brief_parts("NSW RFS", action, topic, [(core, "; ".join(details))],
                       optional, "check rfs.nsw.gov.au", budget, incident.incident_id)



class RFSPoller:
    def __init__(self, db, tx, client=None):
        self.db, self.tx = db, tx
        self.client = client or RFSClient()
        self._poll_lock = asyncio.Lock()
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
                self.next_poll_at = datetime.fromtimestamp(time.time() + interval, timezone.utc).isoformat()
                await asyncio.wait_for(self.event.wait(), interval)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self, *, replay_items=None, force=False):
        async with self._poll_lock:
            started = time.monotonic()
            try:
                return await self._poll_once(replay_items=replay_items, force=force)
            finally:
                if replay_items is None:
                    self.last_poll_duration = round(time.monotonic() - started, 3)

    async def _poll_once(self, *, replay_items=None, force=False):
        settings = self.db.all_settings()
        if not settings.get("rfs_enabled", False):
            self.last_result = "disabled"
            return
        if not settings.get("rfs_all_councils") and not settings.get("rfs_councils"):
            self.last_result = "select councils or All NSW"
            return
        try:
            incidents = replay_items if replay_items is not None else await self.client.fetch()
        except RFSFeedError as exc:
            self.last_poll = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.last_result = f"error: {exc}"
            self.db.add_error("rfs", str(exc))
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        settings = self.db.all_settings()
        if not settings.get("rfs_enabled", False):
            self.last_result = "disabled"
            return
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
            if not self.db.get_setting("rfs_enabled", False):
                self.last_result = "disabled"
                return
            previous = self.db.rfs_get_incident(incident.incident_id)
            if replay_items is None:
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
            if not force and previous and previous["last_sent_hash"] == incident.revision:
                continue
            latest = self.db.rfs_latest_broadcast(incident.incident_id)
            if latest and latest["revision_hash"] == incident.revision and latest["transmit_status"] == "queued":
                continue
            if latest and latest["revision_hash"] == incident.revision and latest["transmit_status"] == "dry-run" and dry_run and not force:
                continue
            action = "UPDATE" if previous and previous["last_sent_hash"] else "NEW"
            try:
                parts = format_incident(incident, budget, action=action)
            except NoticeTooLong as exc:
                if not latest or latest["revision_hash"] != incident.revision or latest["disposition"] != "formatting-blocked":
                    self.db.rfs_add_history(incident, "", "blocked", str(exc), "formatting-blocked")
                    self.db.add_error("rfs", str(exc))
                continue
            text = " || ".join(parts)
            if permanently_unsendable(latest, incident.revision, text):
                continue
            if dry_run:
                self.db.rfs_add_history(incident, text, "dry-run")
                continue
            retry_row = (latest if latest and (not force or latest["transmit_status"] == "deferred") and latest["revision_hash"] == incident.revision
                         and latest["transmitted_text"] == text
                         and latest["transmit_status"] in ("failed", "interrupted", "deferred") else None)
            row_id = retry_row["id"] if retry_row else self.db.rfs_add_history(incident, text, "queued")
            indices = remaining_parts(retry_row, text, parts)
            if not indices:
                self.db.rfs_update_history(row_id, "success")
                self.db.rfs_mark_sent(incident.incident_id, incident.revision)
                continue
            if retry_row:
                self.db.rfs_update_history(row_id, "queued")
            result = {"remaining": len(indices), "ok": True, "error": ""}

            def on_result(index, ok, error="", item=incident, row=row_id, state=result,
                          indexes=tuple(indices), total=len(parts)):
                record_part(self.db, row, indexes[index], total, ok, error)
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

            def valid_if(item=incident):
                current = self.db.all_settings()
                saved = self.db.rfs_get_incident(item.incident_id)
                selected_now = known_nsw if current.get("rfs_all_councils") else {
                    council_key(x) for x in current.get("rfs_councils", [])}
                return bool(current.get("rfs_enabled") and saved and saved["missing_polls"] < 2
                            and saved["revision_hash"] == item.revision
                            and item.level in current.get("rfs_levels", [])
                            and council_key(item.council) in selected_now)

            if submit_notice(self.tx, [parts[i] for i in indices], on_result,
                             priority=0 if incident.level == "Emergency Warning" else 2,
                             valid_if=valid_if):
                queued = True
            else:
                status, reason = queue_refusal(parts)
                self.db.rfs_update_history(row_id, status, reason)
                if status == "failed":
                    self.db.add_error("rfs", reason)
        for missing in ([] if replay_items is not None else self.db.rfs_missing_after_poll({item.incident_id for item in incidents})):
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
        if replay_items is None:
            self.last_successful_poll = self.last_poll
            self.db.set_setting("rfs_last_successful_poll", self.last_poll)

    def _queue_verification(self, dry_run: bool):
        budget = getattr(self.tx, "message_budget", MAX_PAYLOAD_BYTES)
        if len(FINAL_VERIFICATION_MESSAGE.encode()) > budget:
            signature = (len(FINAL_VERIFICATION_MESSAGE.encode()), budget)
            if getattr(self, "_verification_budget_error", None) != signature:
                self._verification_budget_error = signature
                self.db.add_error("rfs", f"verification message exceeds MeshCore limit ({budget} bytes)")
            return
        self._verification_budget_error = None
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
