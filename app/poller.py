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
    MULTIPART_GAP_SECONDS,
    POLL_INTERVAL_MIN,
    POLL_HARD_TIMEOUT,
)
from .dedupe import decide
from .filters import FilterRules
from .formatter import build_mesh_parts
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
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopped:
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
            interval = max(POLL_INTERVAL_MIN, int(self._db.get_setting("poll_interval", 120)))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return

    async def poll_once(self) -> None:
        settings = self._db.all_settings()
        regions = settings.get("bom_regions", ["NSW"])
        contact = settings.get("bom_contact", "")
        self._enricher.contact = contact
        client = BOMClient(contact=contact)
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
        self.status.last_poll_result = f"ok: {len(items)} active BOM warning(s)"

        self._db.purge_expired_state()
        self._db.prune_history()

        rules = FilterRules.from_settings(settings)
        tz_name = settings.get("display_timezone", "Australia/Sydney")
        channel = int(settings.get("meshcore_channel", 0))
        dry_run = bool(settings.get("dry_run", True))

        for item in items:
            try:
                await self._process(item, rules, tz_name, channel, dry_run)
            except Exception as exc:
                logger.exception("error processing BOM warning")
                self._db.add_error("poller", f"process error: {exc}")

    async def _process(self, item, rules, tz_name, channel, dry_run) -> None:
        alert = Alert.from_bom(item)
        if not alert.alert_id:
            return
        decision = decide(alert, rules, self._db.get_state)
        if decision.transmit and decision.disposition != "cancelled" and alert.references:
            enrichment = await self._enricher.enrich(alert.references[0])
            alert.specific_locations = enrichment.locations
            alert.warning_summary = enrichment.summary
        parts = [_format_cancel(alert, tz_name)] if decision.disposition == "cancelled" else build_mesh_parts(alert, tz_name)
        parts.append(FINAL_VERIFICATION_MESSAGE)
        logged_text = " || ".join(parts)

        if not self._db.history_exists(alert.alert_id):
            detail = decision.detail
            history_text = logged_text if decision.transmit else ""
            if decision.transmit and dry_run:
                detail = f"DRY-RUN: {decision.detail}"
            self._db.add_history(alert.alert_id, alert.event, alert.area_desc,
                                 decision.disposition, history_text, detail)

        if not decision.transmit:
            return
        if dry_run:
            for part in parts:
                self._db.add_event("INFO", f"[DRY-RUN] would send: {part}")
            self._record_state(alert, decision)
            return

        fail_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        aggregate = {"remaining": len(parts), "all_ok": True, "first_err": ""}

        def _on_result(ok, err="", a=alert, d=decision, t=logged_text, ts=fail_ts):
            if not ok:
                aggregate["all_ok"] = False
                if not aggregate["first_err"]:
                    aggregate["first_err"] = err or "unknown transmit failure"
            aggregate["remaining"] -= 1
            if aggregate["remaining"] > 0:
                return
            if aggregate["all_ok"]:
                self._record_state(a, d)
                return
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
    from .config import MAX_PAYLOAD_BYTES
    from .formatter import PREFIX, _area_string

    area = _area_string(alert.area_desc)
    body = f"CANCELLED: {alert.event}"
    if area:
        body += f": {area}"
    msg = PREFIX + body
    if len(msg.encode()) <= MAX_PAYLOAD_BYTES:
        return msg
    return (PREFIX + f"CANCELLED: {alert.event}")[:MAX_PAYLOAD_BYTES]
