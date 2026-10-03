"""Live Traffic NSW polling, history decisions, and MeshCore queueing."""
from __future__ import annotations

import asyncio
import time
import logging
from datetime import datetime, timezone

from ..config import FINAL_VERIFICATION_MESSAGE, MAX_PAYLOAD_BYTES, VERIFICATION_INTERVAL_SECONDS, polling_seconds
from ..formatter import compact_topic, format_epoch_until, frame_notice
from ..delivery import permanently_unsendable, queue_refusal, record_part, remaining_parts, submit_notice
from ..rfs.councils import COUNCILS
from ..rfs.feed import council_key
from .feed import SOURCE_URL, TYPES, TrafficClient, TrafficFeedError, match_traffic_council, prepare_councils

logger = logging.getLogger("wx_echo.traffic")


def format_item(item, council: str, budget: int, action: str = "NEW",
                tz_name: str = "Australia/Sydney", council_method: str = "") -> list[str]:
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
    if item.road_details:
        details.append(item.road_details)
    if item.direction:
        details.append(item.direction)
    scheduled = item.feed == "roadwork" or "ROADWORK" in item.category or bool(item.periods)
    if item.impact:
        details.append(("Scheduled impact: " if scheduled else "") + item.impact)
    if scheduled:
        details.append("Schedule: " + "; ".join(p.describe() for p in item.periods) if item.periods else "Closure schedule not supplied")
        state, explanation = item.closure_window()
        details.append(explanation if state == "unknown" else "Scheduled notice; actual closure not confirmed")
    if item.public_transport:
        details.append("Public transport: " + item.public_transport)
    if item.additional_info:
        details.append(item.additional_info)
    if item.advice and item.advice.casefold() not in item.impact.casefold() \
            and item.advice.casefold() not in item.title.casefold():
        details.append(item.advice)
    if council:
        details.append(f"{council} council" + (f" ({council_method})" if council_method else ""))
    end_time = format_epoch_until(item.end, tz_name) if not item.hide_end else ""
    if end_time:
        details.append(end_time)
    return frame_notice("Live Traffic NSW", action, topic,
                        ["; ".join(details) or topic], "check livetraffic.com",
                        budget, item.item_id)


class TrafficPoller:
    def __init__(self, db, tx, client=None):
        self.db, self.tx = db, tx
        self.client = client or TrafficClient()
        self._poll_lock = asyncio.Lock()
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
        if not settings.get("traffic_enabled", False):
            self.last_result = "disabled"
            return
        if not settings.get("traffic_all_councils") and not settings.get("traffic_councils"):
            self.last_result = "select councils or All NSW"
            return
        try:
            requested = set(settings.get("traffic_types", [])) & set(TYPES)
            if settings.get("rfs_enabled"):
                requested.discard("fire")
            if not requested:
                self.last_result = "select hazard feeds (fire feed is suppressed while RFS is enabled)"
                return
            if replay_items is not None:
                items = replay_items
            elif isinstance(self.client, TrafficClient):
                items = await self.client.fetch(requested)
            else:
                items = await self.client.fetch()
            if self._councils is None:
                polygons = await self.client.boundaries()
                self._councils = await asyncio.to_thread(prepare_councils, polygons)
        except (TrafficFeedError, KeyError) as exc:
            self.last_poll = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.last_result = f"error: {exc}"
            self.db.add_error("traffic", str(exc))
            self.db.traffic_record_feed_status(set(), getattr(self.client, "last_errors", {}) or
                                               {feed: str(exc) for feed in requested}, {}, self.last_poll)
            return
        self.last_poll = datetime.now(timezone.utc).isoformat(timespec="seconds")
        settings = self.db.all_settings()
        if not settings.get("traffic_enabled", False):
            self.last_result = "disabled"
            return
        successful_feeds = getattr(self.client, "last_successful_feeds", {item.feed for item in items} or set(TYPES))
        feed_errors = getattr(self.client, "last_errors", {})
        self.last_result = f"{'partial' if feed_errors else 'ok'}: {len(items)} traffic items"
        if replay_items is None:
            self.db.traffic_record_feed_status(successful_feeds, feed_errors,
                                               getattr(self.client, "last_published", {}), self.last_poll)
        if feed_errors:
            self.last_result += "; " + "; ".join(f"{feed}: {error}" for feed, error in feed_errors.items())
        selected = {council_key(x) for x in settings.get("traffic_councils", [])}
        known = {council_key(x) for x in COUNCILS}
        types = set(settings.get("traffic_types", [])) & set(TYPES)
        dry_run = bool(settings.get("dry_run", True))
        baseline_feeds = set(self.db.get_setting("traffic_baseline_feeds", []))
        if self.db.get_setting("traffic_baseline_done", False) and not baseline_feeds:
            baseline_feeds = set(TYPES)  # migrate the previous completed global baseline
        budget = getattr(self.tx, "message_budget", MAX_PAYLOAD_BYTES)
        queued = False
        def match_councils():
            return [match_traffic_council(item, self._councils) for item in items]
        matches = await asyncio.to_thread(match_councils)
        councils = [match.council for match in matches]
        self._matches = {item.item_id: match for item, match in zip(items, matches)}
        def exclusion(item, council):
            if not item.active():
                return "ended or not yet active"
            if item.feed == "fire" and settings.get("rfs_enabled", False):
                return "rfs fire coverage"
            if item.feed not in types or ("ROADWORK" in item.category and "roadwork" not in types):
                return "hazard type"
            if not council or council_key(council) not in (known if settings.get("traffic_all_councils") else selected):
                return "council"
            return ""
        # Choose a selected, matching feed before deduplicating shared provider IDs.
        # The first feed fetched must not hide an enabled flood/fire feed copy.
        preferred = {}
        rank = {"flood": 0, "fire": 1, "roadwork": 2, "incident": 3, "regional": 4}
        for item, council in zip(items, councils):
            if not exclusion(item, council):
                key = item.item_id.partition(":")[2]
                prior = preferred.get(key)
                if prior is None or rank[item.feed] < rank[prior.feed]:
                    preferred[key] = item
        for index, (item, council) in enumerate(zip(items, councils)):
            if index % 20 == 0:
                await asyncio.sleep(0)
            if not self.db.get_setting("traffic_enabled", False):
                self.last_result = "disabled"
                return
            previous = self.db.traffic_get_item(item.item_id)
            active = item.active()
            if replay_items is None:
                self.db.traffic_save_item(item, council, active, matches[index])
            reason = exclusion(item, council)
            if not reason and preferred[item.item_id.partition(":")[2]].item_id != item.item_id:
                reason = "duplicate provider item"
            latest = self.db.latest_service_history("traffic", item.item_id)
            if reason:
                disposition = "excluded-" + reason.replace(" ", "-")
                if not latest or latest["revision_hash"] != item.revision or latest["disposition"] != disposition:
                    self._history(item, council, disposition=disposition, detail=f"Excluded: {reason}")
                continue
            if not force and previous and previous["last_sent_hash"] == item.revision:
                continue
            latest_send = self.db.traffic_latest_broadcast(item.item_id)
            if latest_send and latest_send["revision_hash"] == item.revision:
                if latest_send["transmit_status"] == "queued":
                    continue
                if not force and dry_run and latest_send["transmit_status"] == "dry-run":
                    continue
            action = ("UPDATE" if self.db.latest_successful_broadcast("traffic", item.item_id)
                      else "NEW")
            parts = format_item(item, council, budget, action=action,
                                tz_name=settings.get("display_timezone", "Australia/Sydney"),
                                council_method=matches[index].method)
            message = " || ".join(parts)
            if permanently_unsendable(latest_send, item.revision, message):
                continue
            if dry_run:
                self._history(item, council, text=message, status="dry-run")
                continue
            if not dry_run and not force and item.feed not in baseline_feeds:
                self._history(item, council, text=message, disposition="baseline",
                              detail="Existing item on first live poll; no radio send")
                self.db.traffic_mark_sent(item.item_id, item.revision)
                continue
            retry_row = (latest_send if latest_send and (not force or latest_send["transmit_status"] == "deferred") and latest_send["revision_hash"] == item.revision
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

            def valid_if(event=item, matched_council=council):
                current = self.db.all_settings()
                saved = self.db.traffic_get_item(event.item_id)
                selected_now = known if current.get("traffic_all_councils") else {
                    council_key(x) for x in current.get("traffic_councils", [])}
                return bool(current.get("traffic_enabled") and saved and saved["active"]
                            and saved["revision_hash"] == event.revision and event.active()
                            and event.feed in current.get("traffic_types", [])
                            and ("ROADWORK" not in event.category or "roadwork" in current.get("traffic_types", []))
                            and not (event.feed == "fire" and current.get("rfs_enabled"))
                            and council_key(matched_council) in selected_now)

            if submit_notice(self.tx, [parts[i] for i in indices], on_result, priority=5,
                             valid_if=valid_if):
                queued = True
            else:
                status, reason = queue_refusal(parts)
                self.db.update_service_history(row_id, status, reason)
                if status == "failed":
                    self.db.add_error("traffic", reason)
        for missing in ([] if replay_items is not None else self.db.traffic_missing_after_poll({item.item_id for item in items}, successful_feeds)):
            if missing["missing_polls"] != 2 or not missing["last_sent_hash"]:
                continue
            self.db.add_service_history("traffic", missing["item_id"], missing["title"], missing["council"],
                                        disposition="absent-from-feed", detail="Absent from two successful feed polls; road status unconfirmed",
                                        revision_hash=missing["revision_hash"], metadata={"feed": missing["feed"], "category": missing["category"]})
        if not dry_run and successful_feeds and replay_items is None:
            self.db.set_setting("traffic_baseline_feeds", sorted(baseline_feeds | successful_feeds))
            self.db.set_setting("traffic_baseline_done", True)
        if queued:
            self._queue_verification()
        if replay_items is None:
            self.last_successful_poll = self.last_poll
        if successful_feeds and replay_items is None:
            self.db.set_setting("traffic_last_successful_poll", self.last_poll)

    def _history(self, item, council: str, text: str = "", status=None,
                 disposition: str = "", detail: str = "") -> int:
        match = getattr(self, "_matches", {}).get(item.item_id)
        return self.db.add_service_history(
            "traffic", item.item_id, item.title or item.category, council,
            disposition=disposition, transmitted_text=text, detail=detail,
            transmit_status=status, revision_hash=item.revision,
            metadata={"feed": item.feed, "category": item.category, "road": item.road,
                      "suburb": item.suburb, "direction": item.direction,
                      "impact": item.impact, "advice": item.advice,
                      "road_details": item.road_details, "source_url": SOURCE_URL,
                      "match_method": match.method if match else "unknown",
                      "match_reason": match.reason if match else "",
                      "periods": [p.describe() for p in item.periods],
                      "public_transport": item.public_transport,
                      "additional_info": item.additional_info},
        )

    def _queue_verification(self):
        budget = getattr(self.tx, "message_budget", MAX_PAYLOAD_BYTES)
        if len(FINAL_VERIFICATION_MESSAGE.encode()) > budget:
            signature = (len(FINAL_VERIFICATION_MESSAGE.encode()), budget)
            if getattr(self, "_verification_budget_error", None) != signature:
                self._verification_budget_error = signature
                self.db.add_error("traffic", "verification message exceeds MeshCore limit")
            return
        self._verification_budget_error = None
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
