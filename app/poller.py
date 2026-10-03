"""BOM polling background task."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from .bom import BOMClient, BOMError
from .bom_enricher import BOMEnrichment, BOMWarningEnricher
from .bom_area import match_councils
from .config import (
    FINAL_VERIFICATION_MESSAGE,
    MAX_PAYLOAD_BYTES,
    polling_seconds,
    POLL_HARD_TIMEOUT,
    VERIFICATION_INTERVAL_SECONDS,
)
from .dedupe import Decision, decide
from .delivery import permanently_unsendable, queue_refusal, record_part, remaining_parts, submit_notice
from .filters import FilterRules, should_include
from .formatter import build_mesh_parts, frame_notice, marine_notice_sections, bom_notice_sections
from .models import Alert
from .rfs.feed import council_key
from .traffic.feed import TrafficClient, prepare_councils

logger = logging.getLogger("wx_echo.poller")


class PollerStatus:
    def __init__(self):
        self.last_poll_time: str | None = None
        self.last_poll_success_time: str | None = None
        self.last_poll_result: str = "not yet polled"
        self.last_raw_response: str = ""
        self.last_broadcast_failure: str | None = None
        self.last_broadcast_failure_text: str = ""
        self.clock_skew_seconds: float | None = None
        self.started_at: datetime = datetime.now(timezone.utc)

    @property
    def uptime_seconds(self) -> int:
        return int((datetime.now(timezone.utc) - self.started_at).total_seconds())


class BomPoller:
    def __init__(self, db, transmit_manager):
        self._db = db
        self._tx = transmit_manager
        self.status = PollerStatus()
        self._poll_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._wake_generation = 0
        self._stopped = False
        self._enricher = BOMWarningEnricher()
        self._council_index = None

    def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name="bom-poller")

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def poke(self) -> None:
        self._wake_generation += 1
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopped:
            generation = self._wake_generation
            try:
                await asyncio.wait_for(self.poll_once(), timeout=POLL_HARD_TIMEOUT)
            except asyncio.TimeoutError:
                self.status.last_poll_result = "error: poll timed out (aborted by watchdog)"
                self._db.add_error("poller", "poll hung and was aborted after %ds" % POLL_HARD_TIMEOUT)
                logger.error("poll_once exceeded %ds; aborted by watchdog", POLL_HARD_TIMEOUT)
            except Exception as exc:
                self.status.last_poll_result = f"error: {exc}"
                self._db.add_error("poller", str(exc))
                logger.exception("unexpected poll error")
            if self._wake_generation != generation:
                continue
            interval = polling_seconds(self._db.get_setting("bom_poll_minutes", 5), 5)
            self._wake.clear()
            self.next_poll_at = datetime.fromtimestamp(time.time() + interval, timezone.utc).isoformat()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return

    async def poll_once(self):
        async with self._poll_lock:
            started = time.monotonic()
            try:
                return await self._poll_once()
            finally:
                self.last_poll_duration = round(time.monotonic() - started, 3)

    async def _poll_once(self) -> None:
        settings = self._db.all_settings()
        if not settings.get("bom_enabled", True):
            self.status.last_poll_result = "disabled"
            return
        regions = ["NSW"]
        client = BOMClient()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            items, raw = await client.fetch_active(
                regions)
        except BOMError as exc:
            self.status.last_poll_time = now
            self.status.last_poll_result = f"error: {exc}"
            self._db.add_error("bom", str(exc))
            self._db.add_event("ERROR", f"BOM poll failed: {exc}")
            return

        if not self._db.get_setting("bom_enabled", True):
            self.status.last_poll_result = "disabled"
            return
        self.status.last_poll_time = now
        self.status.last_poll_success_time = now
        self.status.last_raw_response = raw
        server_date = getattr(client, "last_server_date", None)
        if server_date:
            try:
                from email.utils import parsedate_to_datetime
                server_dt = parsedate_to_datetime(server_date)
                self.status.clock_skew_seconds = (
                    datetime.now(timezone.utc) - server_dt).total_seconds()
            except Exception:
                pass
        if client.last_errors:
            detail = "; ".join(client.last_errors)
            self.status.last_poll_result = f"partial: {len(items)} active BOM warning(s); {detail}"
            self._db.add_error("bom", "partial poll: %s" % detail)
        else:
            self.status.last_poll_result = f"ok: {len(items)} active BOM warning(s)"

        successful = getattr(client, "last_successful_regions", None)
        if successful is None and not client.last_errors:
            from .bom import BOM_FEEDS
            successful = set(regions or BOM_FEEDS)
            for item in items:
                item.setdefault("region", next(iter(successful), ""))
        items = [item for item in items if item.get("region", "NSW") == "NSW"]
        for item in items:
            item.setdefault("region", "NSW")
        self._db.purge_expired_state()
        self._db.prune_history()

        rules = FilterRules.from_settings(settings)
        tz_name = settings.get("display_timezone", "Australia/Sydney")
        channel = int(settings.get("meshcore_channel", 0))
        dry_run = bool(settings.get("dry_run", True))

        queued_warning = False
        for item in items:
            if not self._db.get_setting("bom_enabled", True):
                break
            try:
                queued_warning |= bool(await self._process(item, rules, tz_name, channel, dry_run,
                                                           settings))
            except Exception as exc:
                item["selection"] = "processing error"
                logger.exception("error processing BOM warning")
                self._db.add_error("poller", f"process error: {exc}")
        self._db.replace_bom_current(items, set(successful or ()) & {"NSW"}, now)
        if queued_warning and self._db.get_setting("bom_enabled", True):
            self._queue_verification(channel, dry_run)

    async def _process(self, item, rules, tz_name, channel, dry_run,
                       settings=None, force=False) -> None:
        if item.get("region", "NSW") != "NSW":
            return False
        alert = Alert.from_bom(item)
        if not alert.alert_id:
            return False
        settings = settings or self._db.all_settings()
        enrichment = None
        if alert.message_type != "Cancel" and alert.references:
            enrichment = await self._enricher.enrich(alert.references[0])
            alert.specific_locations = enrichment.locations
            alert.warning_summary = enrichment.summary
            alert.warning_sections = (getattr(enrichment, "sections", ())
                                      if alert.event == "Marine Wind Warning" else ())
            alert.effective = getattr(enrichment, "issued", "") or alert.effective
            alert.expires = getattr(enrichment, "expires", "") or alert.expires
        item["_enrichment"] = asdict(enrichment) if isinstance(enrichment, BOMEnrichment) else {}
        item.update(effective=alert.effective, expires=alert.expires)
        item["enrichment_status"] = (getattr(enrichment, "status", "") or
                                      ("available" if enrichment and enrichment != BOMEnrichment() else "unavailable")) if alert.references and alert.message_type != "Cancel" else "not requested"
        item["provider_areas"] = [{"type": kind, "code": code, "name": name}
                                  for kind, code, name in getattr(enrichment, "geocodes", ())]
        item.update(specific_locations=alert.specific_locations,
                    warning_summary=alert.warning_summary,
                    warning_sections=[{"phenomenon": s.phenomenon, "areas": s.areas,
                                       "phase": s.phase, "onset": s.onset}
                                      for s in alert.warning_sections])
        if not self._db.get_setting("bom_enabled", True):
            return False
        decision = decide(alert, rules, self._db.get_state)
        selected = set(settings.get("bom_councils", []))
        all_councils = bool(settings.get("bom_all_councils", True))
        include_unknown = bool(settings.get("bom_include_unknown_councils", True))
        polygons = tuple(getattr(enrichment, "polygons", ()) or ())
        if polygons and self._council_index is None:
            boundaries = await TrafficClient().boundaries()
            self._council_index = await asyncio.to_thread(prepare_councils, boundaries)
        match = await asyncio.to_thread(
            match_councils, alert.area_desc,
            tuple(getattr(enrichment, "area_names", ()) or ()),
            polygons, self._council_index, tuple(getattr(enrichment, "lga_names", ()) or ()))
        matched_selected = bool({council_key(x) for x in match.councils} &
                                {council_key(x) for x in selected})
        latest = self._db.latest_history(alert.alert_id)
        previous_meta = {}
        if latest is not None:
            raw_meta = latest["metadata"]
            previous_meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
        selected_keys = {council_key(x) for x in selected}
        prior_keys = {council_key(x) for x in previous_meta.get("selected_councils", [])}
        expanded = (previous_meta.get("all_councils") is False and all_councils)
        expanded |= bool(match.status == "matched" and
                         {council_key(x) for x in match.councils} & (selected_keys - prior_keys))
        expanded |= bool(match.status == "unknown" and include_unknown and
                         previous_meta.get("include_unknown") is False)
        if (decision.disposition == "duplicate" and previous_meta and expanded):
            decision = Decision("update", True, "newly selected BOM council coverage")
        eligible = should_include(alert.event, rules)
        try:
            expiry = datetime.fromisoformat(alert.expires) if alert.expires else None
            expired = expiry is not None and expiry.tzinfo is not None and expiry <= datetime.now(timezone.utc)
        except ValueError:
            expiry = None
            expired = False
        districts = settings.get("bom_districts", [])
        haystack = " ".join((alert.event, alert.area_desc, alert.headline, alert.detail,
                             alert.specific_locations, alert.warning_summary)).casefold()
        district_match = not districts or any(d.strip().casefold() in haystack
                                              for d in districts if d.strip())
        if not district_match and decision.disposition != "cancelled":
            eligible = False
            decision = Decision("filtered", False, "outside selected BOM districts")
        if expired and alert.message_type != "Cancel":
            eligible = False
            decision = Decision("filtered", False, "BOM warning has expired")
        if not all_councils and decision.disposition != "cancelled":
            if not selected:
                eligible = False
                decision.transmit = False
                decision.disposition = "filtered"
                decision.detail = "no BOM councils selected"
            elif match.status == "matched" and not matched_selected:
                eligible = False
                decision.transmit = False
                decision.disposition = "filtered"
                decision.detail = "outside selected BOM councils"
            elif match.status == "unknown" and not include_unknown:
                eligible = False
                decision.transmit = False
                decision.disposition = "filtered"
                decision.detail = "BOM council match unknown"
        if force and eligible and alert.message_type != "Cancel":
            decision = Decision("update", True, "Operator requested resend of current warning")
        selection = "included" if eligible or decision.disposition == "cancelled" else "excluded"
        if alert.message_type == "Cancel" and decision.disposition == "filtered":
            selection = "excluded"
        item.update(council_match=match.status, matched_councils=list(match.councils),
                    match_method=match.method, match_reason=match.reason, selection=selection,
                    selection_reason=decision.detail)
        area_detail = (f"; council match: {match.status}"
                       + (f" ({', '.join(match.councils)})" if match.councils else "")
                       + (f" via {match.method}" if match.method else ""))
        budget = getattr(self._tx, "message_budget", MAX_PAYLOAD_BYTES)
        topic = alert.event
        if decision.disposition == "cancelled":
            topic = re.sub(r"^Cancellation of\s+", "", topic, flags=re.I)
            body_parts = [_format_cancel(alert, tz_name)]
            action = "CANCELLED"
        else:
            action = "UPDATE" if decision.disposition == "update" or (latest is not None and (latest["revision_hash"].partition(":")[0] != alert.revision_hash() or latest["disposition"] == "update")) else "NEW"
            if alert.warning_sections or re.search(r"\bCANCELLED\b", alert.warning_summary or alert.detail or "", re.I):
                body_parts = bom_notice_sections(alert, tz_name, action)
            else:
                body_parts = build_mesh_parts(alert, tz_name, max_bytes=budget, split=False)
                if body_parts:
                    first = re.sub(r"^\d+/\d+\s+", "", body_parts[0])
                    if first.startswith(topic):
                        body_parts[0] = first[len(topic):].lstrip()
        parts = frame_notice("BOM", action, topic, body_parts,
                             "check bom.gov.au", budget, alert.alert_id)
        logged_text = " || ".join(parts)

        coverage = json.dumps([all_councils, sorted(selected), include_unknown,
                               sorted(districts), rules.include_exact,
                               rules.include_suffix, rules.exclude_exact],
                              separators=(",", ":"))
        coverage_hash = hashlib.sha256(coverage.encode()).hexdigest()[:8]
        revision_hash = f"{alert.revision_hash()}:{coverage_hash}"
        if permanently_unsendable(latest, revision_hash, logged_text):
            return False
        if (decision.transmit and latest is not None
                and latest["revision_hash"] == revision_hash
                and (latest["transmit_status"] == "queued" or
                     (not force and not dry_run and latest["transmit_status"] == "success"))):
            return False
        retry_existing = ((not force or (latest is not None and latest["transmit_status"] == "deferred")) and decision.transmit and not dry_run and latest is not None
                          and latest["revision_hash"] == revision_hash
                          and latest["transmitted_text"] == logged_text
                          and latest["transmit_status"] in ("failed", "interrupted", "deferred"))
        refreshed_preview = bool(dry_run and decision.transmit and latest is not None
                                 and latest["transmit_status"] == "dry-run"
                                 and latest["revision_hash"] == revision_hash
                                 and latest["transmitted_text"] != logged_text)
        if refreshed_preview or (force and decision.transmit and not retry_existing) or latest is None or latest["revision_hash"] != revision_hash or (
                decision.transmit and not dry_run and not retry_existing
                and latest["transmit_status"] in ("failed", "interrupted", "deferred")):
            detail = decision.detail + area_detail
            history_text = logged_text if decision.transmit else ""
            transmit_status = "queued" if decision.transmit else None
            if decision.transmit and dry_run:
                detail = f"DRY-RUN: {decision.detail}{area_detail}"
                transmit_status = "dry-run"
            disposition = latest["disposition"] if refreshed_preview else decision.disposition
            if not refreshed_preview and latest is not None and disposition == "sent":
                disposition = "update"
            history_id = self._db.add_history(
                alert.alert_id, alert.event, alert.area_desc, disposition,
                history_text, detail, transmit_status=transmit_status,
                revision_hash=revision_hash,
                metadata={"region": "NSW", "council_match": match.status,
                          "matched_councils": list(match.councils),
                          "match_method": match.method, "selection": selection,
                          "match_reason": match.reason, "enrichment_status": item["enrichment_status"],
                          "source_url": alert.references[0] if alert.references else "",
                          "provider_areas": item["provider_areas"],
                          "all_councils": all_councils,
                          "selected_councils": sorted(selected),
                          "include_unknown": include_unknown},
            )
        else:
            history_id = latest["id"]
            if decision.transmit and dry_run:
                self._db.refresh_dry_run_history_text(history_id, logged_text)
            if decision.transmit and not dry_run:
                self._db.update_history_transmit_status(
                    history_id, "queued", decision.detail,
                )

        if not decision.transmit:
            return False
        if dry_run:
            for part in parts:
                self._db.add_event("INFO", f"[DRY-RUN] would send: {part}")
            return True

        indices = remaining_parts(latest if retry_existing else None, logged_text, parts)
        if not indices:
            self._db.update_history_transmit_status(history_id, "success")
            self._record_state(alert, decision)
            return False
        fail_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        aggregate = {"remaining": len(indices), "all_ok": True, "first_err": ""}

        def _on_result(index, ok, err="", a=alert, d=decision, t=logged_text,
                       ts=fail_ts, row_id=history_id, indexes=tuple(indices), total=len(parts)):
            record_part(self._db, row_id, indexes[index], total, ok, err)
            if not ok:
                aggregate["all_ok"] = False
                if not aggregate["first_err"]:
                    aggregate["first_err"] = err or "unknown transmit failure"
            aggregate["remaining"] -= 1
            if aggregate["remaining"] > 0:
                return
            if aggregate["all_ok"]:
                self._db.update_history_transmit_status(row_id, "success")
                self._record_state(a, d)
                return
            self._db.update_history_transmit_status(
                row_id, "failed",
                "%s; broadcast failed: %s" % (d.detail, aggregate["first_err"]),
            )
            self.status.last_broadcast_failure = ts
            self.status.last_broadcast_failure_text = t
            self._db.add_error("broadcast", f"NOT SENT on MeshCore (will retry): {aggregate['first_err']} :: {t}")
            self._db.add_event("ALARM", f"BROADCAST FAILED, will retry: {a.event} for {(a.area_desc or '')[:40]}")
            logger.error("broadcast FAILED on MeshCore: %s", t)

        prepared_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def valid_if():
            current = self._db.all_settings()
            latest_now = self._db.latest_history(alert.alert_id)
            if not current.get("bom_enabled", True) or not latest_now or latest_now["revision_hash"] != revision_hash:
                return False
            if decision.disposition == "cancelled":
                return True
            if expiry is not None and expiry.tzinfo is not None and expiry <= datetime.now(timezone.utc):
                return False
            snapshots = self._db.bom_snapshot_regions(["NSW"])
            if snapshots and snapshots[0]["fetched_at"] > prepared_at and not self._db.bom_current_item(alert.alert_id):
                return False
            if not should_include(alert.event, FilterRules.from_settings(current)):
                return False
            selected_districts = current.get("bom_districts", [])
            if selected_districts and not any(d.strip().casefold() in haystack for d in selected_districts if d.strip()):
                return False
            if current.get("bom_all_councils", True):
                return True
            selected_now = {council_key(x) for x in current.get("bom_councils", [])}
            return bool(selected_now and (
                match.status == "unknown" and current.get("bom_include_unknown_councils", True)
                or {council_key(x) for x in match.councils} & selected_now))

        if not submit_notice(self._tx, [parts[i] for i in indices], _on_result, priority=1,
                             valid_if=valid_if):
            status, reason = queue_refusal(parts)
            self._db.update_history_transmit_status(history_id, status, reason)
            if status == "failed":
                self._db.add_error("broadcast", reason)
            return False
        self._db.add_event("INFO", f"queued {decision.disposition} ({len(parts)} part): {logged_text}")
        logger.info("alert %s -> %s%s", alert.alert_id, decision.disposition,
                    " (dry-run)" if dry_run else "",
                    extra={"alert_id": alert.alert_id,
                           "disposition": decision.disposition,
                           "action": "dry-run" if dry_run else "queued"})
        return True

    def _queue_verification(self, channel: int, dry_run: bool) -> None:
        budget = getattr(self._tx, "message_budget", MAX_PAYLOAD_BYTES)
        length = len(FINAL_VERIFICATION_MESSAGE.encode("utf-8"))
        if length > budget:
            signature = (len(FINAL_VERIFICATION_MESSAGE.encode()), budget)
            if getattr(self, "_verification_budget_error", None) != signature:
                self._verification_budget_error = signature
                detail = f"verification message exceeds MeshCore limit ({length} > {budget} bytes)"
                self._db.add_error("broadcast", detail)
                self._db.add_event("WARN", detail)
            return
        self._verification_budget_error = None
        key = "verification_dry_run_last_ts" if dry_run else "verification_live_last_ts"
        timestamps = self._db.get_setting(key, {}) or {}
        last = timestamps.get(str(channel), "")
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        except (TypeError, ValueError):
            elapsed = VERIFICATION_INTERVAL_SECONDS
        due = elapsed >= VERIFICATION_INTERVAL_SECONDS
        if dry_run:
            if due:
                self._db.add_event("INFO", f"[DRY-RUN] would send: {FINAL_VERIFICATION_MESSAGE}")
                timestamps[str(channel)] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._db.set_setting(key, timestamps)
            return

        def on_result(ok: bool, err: str = "") -> None:
            if ok:
                saved = self._db.get_setting(key, {}) or {}
                saved[str(channel)] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._db.set_setting(key, saved)
                self._db.add_event("INFO", "verification message delivered")
            else:
                self._db.add_error("broadcast", f"verification message failed: {err}")
                self._db.add_event("WARN", f"verification message failed: {err}")

        self._tx.enqueue_verification(FINAL_VERIFICATION_MESSAGE, on_result=on_result,
                                      allow_new=due)

    def _record_state(self, alert: Alert, decision) -> None:
        self._db.upsert_state(
            alert_id=alert.alert_id,
            event=alert.event,
            headline=alert.headline,
            expires=alert.expires,
            msg_hash=alert.content_hash(),
            disposition=decision.disposition,
            sent_ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


def _format_cancel(alert: Alert, tz_name: str) -> str:
    from .formatter import _area_string

    area = _area_string(alert.area_desc)
    return f"for {area}" if area else "warning cancelled"
