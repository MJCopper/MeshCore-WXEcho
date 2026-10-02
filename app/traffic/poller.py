"""Live Traffic NSW polling, history decisions, and MeshCore queueing."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..config import FINAL_VERIFICATION_MESSAGE, MAX_PAYLOAD_BYTES, VERIFICATION_INTERVAL_SECONDS, polling_seconds
from ..formatter import compact_topic, format_epoch_until, frame_notice
from ..delivery import permanently_unsendable, queue_refusal, record_part, remaining_parts, submit_notice
from ..rfs.councils import COUNCILS
from ..rfs.feed import council_key
from .feed import SOURCE_URL, TYPES, TrafficClient, TrafficFeedError, council_at_prepared, prepare_councils

logger = logging.getLogger("wx_echo.traffic")


def format_item(item, council: str, budget: int, action: str = "NEW",
                tz_name: str = "Australia/Sydney") -> list[str]:
    topic = item.category or item.title or "Traffic notice"
    if item.road:
        topic += f" {item.road}"
    topic, shortened = compact_topic(topic, "Live Traffic NSW", action, budget,
                                     "check livetraffic.com")
    details = [item.road] if shortened and item.road else []
    if item.title and item.title.casefold() not in {item.category.casefold(), topic.casefold()}:
        details.append(item.title)
    place = item.suburb if item.road else ", ".join(x for x in (item.road, item.suburb) if x)
    if place:
        details.append(place)
    if item.direction:
        details.append(item.direction)
    if item.impact:
        details.append(item.impact)
    if item.advice and item.advice.casefold() not in item.impact.casefold() \
            and item.advice.casefold() not in item.title.casefold():
        details.append(item.advice)
    if council:
        details.append(f"{council} council")
    end_time = format_epoch_until(item.end, tz_name)
    if end_time:
        details.append(end_time)
    return frame_notice("Live Traffic NSW", action, topic,
                        ["; ".join(details) or topic], "check livetraffic.com",
                        budget, item.item_id)


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
            action = ("UPDATE" if self.db.latest_successful_broadcast("traffic", item.item_id)
                      else "NEW")
            parts = format_item(item, council, budget, action=action,
                                tz_name=settings.get("display_timezone", "Australia/Sydney"))
            message = " || ".join(parts)
            if permanently_unsendable(latest_send, item.revision, message):
                continue
            if dry_run:
                self._history(item, council, text=message, status="dry-run")
                continue
            if first_live_poll:
                self._history(item, council, text=message, disposition="baseline",
                              detail="Existing item on first live poll; no radio send")
                self.db.traffic_mark_sent(item.item_id, item.revision)
                continue
            retry_row = (latest_send if latest_send and latest_send["revision_hash"] == item.revision
                         and latest_send["transmitted_text"] == message
                         and latest_send["transmit_status"] in ("failed", "interrupted", "deferred") else None)
            row_id = retry_row["id"] if retry_row else self._history(item, council, text=message, status="queued")
            indices = remaining_parts(retry_row, message, parts)
            if not indices:
                self.db.update_service_history(row_id, "success")
                self.db.traffic_mark_sent(item.item_id, item.revision)
                continue
            if retry_row:
                self.db.update_service_history(row_id, "queued")
            state = {"remaining": len(indices), "ok": True, "error": ""}

            def on_result(index, ok, error="", event=item, row=row_id, result=state,
                          indexes=tuple(indices), total=len(parts)):
                record_part(self.db, row, indexes[index], total, ok, error)
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

            if submit_notice(self.tx, [parts[i] for i in indices], on_result, priority=5):
                queued = True
            else:
                status, reason = queue_refusal(parts)
                self.db.update_service_history(row_id, status, reason)
                if status == "failed":
                    self.db.add_error("traffic", reason)
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
