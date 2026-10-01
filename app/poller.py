"""BOM polling background task."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .bom import BOMClient, BOMError
from .bom_enricher import BOMWarningEnricher
from .config import (
    BURST_GAP_SECONDS,
    FINAL_VERIFICATION_MESSAGE,
    MAX_PAYLOAD_BYTES,
    MULTIPART_GAP_SECONDS,
    polling_seconds,
    POLL_HARD_TIMEOUT,
    VERIFICATION_INTERVAL_SECONDS,
)
from .dedupe import decide
from .filters import FilterRules, should_include
from .formatter import build_mesh_parts, _split_complete_message, append_source_note
from .models import Alert

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
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._wake_generation = 0
        self._stopped = False
        self._enricher = BOMWarningEnricher()

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
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return

    async def poll_once(self) -> None:
        settings = self._db.all_settings()
        if not settings.get("bom_enabled", True):
            self.status.last_poll_result = "disabled"
            return
        regions = settings.get("bom_regions", ["NSW"])
        client = BOMClient()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            items, raw = await client.fetch_active(
                regions, districts=settings.get("bom_districts", []))
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
        self._db.replace_bom_current(items, set(successful or ()), now)

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
                queued_warning |= bool(await self._process(item, rules, tz_name, channel, dry_run))
            except Exception as exc:
                logger.exception("error processing BOM warning")
                self._db.add_error("poller", f"process error: {exc}")
        if queued_warning and self._db.get_setting("bom_enabled", True):
            self._queue_verification(channel, dry_run)

    async def _process(self, item, rules, tz_name, channel, dry_run) -> None:
        alert = Alert.from_bom(item)
        if not alert.alert_id:
            return
        if alert.message_type != "Cancel" and alert.references and should_include(alert.event, rules):
            enrichment = await self._enricher.enrich(alert.references[0])
            alert.specific_locations = enrichment.locations
            alert.warning_summary = enrichment.summary
            alert.warning_sections = (getattr(enrichment, "sections", ())
                                      if alert.event == "Marine Wind Warning" else ())
        if not self._db.get_setting("bom_enabled", True):
            return False
        decision = decide(alert, rules, self._db.get_state)
        budget = getattr(self._tx, "message_budget", MAX_PAYLOAD_BYTES)
        source_note = "; check bom.gov.au"
        body_budget = budget - len(source_note.encode("utf-8"))
        if body_budget <= 12:
            raise ValueError("MeshCore message budget too small for BOM source note")
        body_parts = (_split_complete_message(_format_cancel(alert, tz_name), body_budget)
                      if decision.disposition == "cancelled"
                      else build_mesh_parts(alert, tz_name, max_bytes=body_budget))
        parts = append_source_note(body_parts, source_note, budget)
        logged_text = " || ".join(parts)

        revision_hash = alert.revision_hash()
        latest = self._db.latest_history(alert.alert_id)
        if latest is None or latest["revision_hash"] != revision_hash:
            detail = decision.detail
            history_text = logged_text if decision.transmit else ""
            transmit_status = "queued" if decision.transmit else None
            if decision.transmit and dry_run:
                detail = f"DRY-RUN: {decision.detail}"
                transmit_status = "dry-run"
            disposition = decision.disposition
            if latest is not None and disposition == "sent":
                disposition = "update"
            history_id = self._db.add_history(
                alert.alert_id, alert.event, alert.area_desc, disposition,
                history_text, detail, transmit_status=transmit_status,
                revision_hash=revision_hash,
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

        fail_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        aggregate = {"remaining": len(parts), "all_ok": True, "first_err": ""}

        def _on_result(ok, err="", a=alert, d=decision, t=logged_text, ts=fail_ts, row_id=history_id):
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

        for idx, part in enumerate(parts):
            delay_after = MULTIPART_GAP_SECONDS if idx < (len(parts) - 1) else BURST_GAP_SECONDS
            self._tx.enqueue(part, channel, on_result=_on_result, delay_after=delay_after)
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
            detail = f"verification message exceeds MeshCore limit ({length} > {budget} bytes)"
            self._db.add_error("broadcast", detail)
            self._db.add_event("WARN", detail)
            return
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
    from .formatter import PREFIX, _area_string

    area = _area_string(alert.area_desc)
    body = f"CANCELLED: {alert.event}"
    if area:
        body += f": {area}"
    return PREFIX + body
