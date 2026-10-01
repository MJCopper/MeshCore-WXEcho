"""Live Traffic NSW polling, history decisions, and MeshCore queueing."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..config import FINAL_VERIFICATION_MESSAGE, MAX_PAYLOAD_BYTES, QUEUE_MAX, VERIFICATION_INTERVAL_SECONDS, polling_seconds
from ..formatter import _split_complete_message, append_source_note
from ..rfs.councils import COUNCILS
from ..rfs.feed import council_key
from .feed import SOURCE_URL, TYPES, TrafficClient, TrafficFeedError, council_at_prepared, prepare_councils

logger = logging.getLogger("wx_echo.traffic")


def format_item(item, council: str, budget: int) -> list[str]:
    place = ", ".join(x for x in (item.road, item.suburb) if x)
    body = f"Live Traffic NSW {item.title or item.category}"
    if place:
        body += f"; {place}"
    if item.direction:
        body += f"; {item.direction}"
    if item.impact:
        body += f"; {item.impact}"
    body += f"; {council} council"
    return append_source_note(_split_complete_message(body, budget), "; check livetraffic.com", budget)


class TrafficPoller:
    def __init__(self, db, tx, client=None):
        self.db, self.tx = db, tx
        self.client = client or TrafficClient()
        self.task = None
        self.event = asyncio.Event()
        self._poke_generation = 0
        self.last_poll = ""
        self.last_successful_poll = ""
        self.last_result = "not polled"
        self._councils = None

    def start(self):
        recovered = self.db.traffic_recover_queued()
        if recovered:
            logger.warning("Recovered %s interrupted traffic sends", recovered)
        self.task = asyncio.create_task(self._run(), name="traffic-poller")

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
                logger.exception("Traffic poll failed")
                self.last_poll = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.last_result = f"error: {exc}"
                self.db.add_error("traffic", str(exc))
            if self._poke_generation != generation:
                continue
            self.event.clear()
            try:
                interval = polling_seconds(self.db.get_setting("traffic_poll_minutes", 10), 10)
                await asyncio.wait_for(self.event.wait(), interval)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self):
        settings = self.db.all_settings()
        if not settings.get("traffic_enabled", False):
            self.last_result = "disabled"
            return
        if not settings.get("traffic_all_councils") and not settings.get("traffic_councils"):
            self.last_result = "select councils or All NSW"
            return
        try:
            items = await self.client.fetch()
            if self._councils is None:
                polygons = await self.client.boundaries()
                self._councils = await asyncio.to_thread(prepare_councils, polygons)
        except (TrafficFeedError, KeyError) as exc:
            self.last_poll = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.last_result = f"error: {exc}"
            self.db.add_error("traffic", str(exc))
            return
        self.last_poll = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.last_result = f"ok: {len(items)} traffic items"
        selected = {council_key(x) for x in settings.get("traffic_councils", [])}
        known = {council_key(x) for x in COUNCILS}
        types = set(settings.get("traffic_types", [])) & set(TYPES)
        dry_run = bool(settings.get("dry_run", True))
        first_live_poll = not self.db.get_setting("traffic_baseline_done", False) and not dry_run
        budget = getattr(self.tx, "message_budget", MAX_PAYLOAD_BYTES)
        queued = False
        def match_councils():
            return [(council_at_prepared(item.lon, item.lat, self._councils)
                     if item.lon is not None and item.lat is not None else "") or item.council
                    for item in items]
        councils = await asyncio.to_thread(match_councils)
        for index, (item, council) in enumerate(zip(items, councils)):
            if index % 20 == 0:
                await asyncio.sleep(0)
            previous = self.db.traffic_get_item(item.item_id)
            active = item.active()
            self.db.traffic_save_item(item, council, active)
            reason = ""
            if not active:
                reason = "ended or not yet active"
            elif item.feed == "fire" and settings.get("rfs_enabled", False):
                reason = "rfs fire coverage"
            elif item.feed not in types or ("ROADWORK" in item.category and "roadwork" not in types):
                reason = "hazard type"
            elif not council or council_key(council) not in (known if settings.get("traffic_all_councils") else selected):
                reason = "council"
            latest = self.db.latest_service_history("traffic", item.item_id)
            if reason:
                disposition = "excluded-" + reason.replace(" ", "-")
                if not latest or latest["revision_hash"] != item.revision or latest["disposition"] != disposition:
                    self._history(item, council, disposition=disposition, detail=f"Excluded: {reason}")
                continue
            if previous and previous["last_sent_hash"] == item.revision:
                continue
            latest_send = self.db.traffic_latest_broadcast(item.item_id)
            if latest_send and latest_send["revision_hash"] == item.revision:
                if latest_send["transmit_status"] == "queued":
                    continue
                if dry_run and latest_send["transmit_status"] == "dry-run":
                    continue
            parts = format_item(item, council, budget)
            message = " || ".join(parts)
            if dry_run:
                self._history(item, council, text=message, status="dry-run")
                continue
            if first_live_poll:
                self._history(item, council, text=message, disposition="baseline",
                              detail="Existing item on first live poll; no radio send")
                self.db.traffic_mark_sent(item.item_id, item.revision)
                continue
            queue_depth = getattr(self.tx, "queue_depth", lambda: 0)()
            if queue_depth + len(parts) > QUEUE_MAX - 4:
                if not latest or latest["revision_hash"] != item.revision or latest["disposition"] != "deferred-queue":
                    self._history(item, council, text=message, disposition="deferred-queue",
                                  detail="Waiting for space in the MeshCore send queue")
                continue
            row_id = self._history(item, council, text=message, status="queued")
            state = {"remaining": len(parts), "ok": True, "error": ""}

            def on_result(ok, error="", event=item, row=row_id, result=state):
                result["remaining"] -= 1
                if not ok:
                    result["ok"] = False
                    result["error"] = result["error"] or error or "send failed"
                if result["remaining"]:
                    return
                if result["ok"]:
                    self.db.update_service_history(row, "success")
                    self.db.traffic_mark_sent(event.item_id, event.revision)
                else:
                    self.db.update_service_history(row, "failed", result["error"])
                    self.db.add_error("traffic", f"Traffic broadcast failed: {event.title}: {result['error']}")

            for part in parts:
                self.tx.enqueue(part, on_result=on_result, delay_after=3)
            queued = True
        for missing in self.db.traffic_missing_after_poll({item.item_id for item in items}):
            if missing["missing_polls"] != 2 or not missing["last_sent_hash"]:
                continue
            self.db.add_service_history("traffic", missing["item_id"], missing["title"], missing["council"],
                                        disposition="absent-from-feed", detail="Absent from two successful feed polls; road status unconfirmed",
                                        revision_hash=missing["revision_hash"], metadata={"feed": missing["feed"], "category": missing["category"]})
        if not self.db.get_setting("traffic_baseline_done", False):
            self.db.set_setting("traffic_baseline_done", True)
        if queued:
            self._queue_verification()
        self.last_successful_poll = self.last_poll

    def _history(self, item, council: str, text: str = "", status=None,
                 disposition: str = "", detail: str = "") -> int:
        return self.db.add_service_history(
            "traffic", item.item_id, item.title or item.category, council,
            disposition=disposition, transmitted_text=text, detail=detail,
            transmit_status=status, revision_hash=item.revision,
            metadata={"feed": item.feed, "category": item.category, "road": item.road,
                      "suburb": item.suburb, "source_url": SOURCE_URL},
        )

    def _queue_verification(self):
        budget = getattr(self.tx, "message_budget", MAX_PAYLOAD_BYTES)
        if len(FINAL_VERIFICATION_MESSAGE.encode()) > budget:
            self.db.add_error("traffic", "verification message exceeds MeshCore limit")
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
                self.db.add_error("traffic", f"verification message failed: {error}")

        self.tx.enqueue_verification(FINAL_VERIFICATION_MESSAGE, on_result=on_result,
                                     allow_new=elapsed >= VERIFICATION_INTERVAL_SECONDS)
