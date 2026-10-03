import pytest
from datetime import datetime, timedelta, timezone

from app.config import BURST_GAP_SECONDS, FINAL_VERIFICATION_MESSAGE, MULTIPART_GAP_SECONDS
from app.filters import FilterRules
from app.poller import BomPoller

TEST_EXPIRY = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()


class _FakeDb:
    def __init__(self):
        self.state_rows = []
        self.history_rows = []
        self.events = []
        self.errors = []
        self.settings = {}

    def all_settings(self):
        return dict(self.settings)

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def get_state(self, alert_id):
        return None

    def latest_history(self, alert_id):
        for row in reversed(self.history_rows):
            if row["alert_id"] == alert_id:
                return row
        return None

    def add_history(self, alert_id, event, area, disposition, transmitted_text="", detail="",
                    transmit_status=None, revision_hash="", metadata=None):
        row_id = len(self.history_rows) + 1
        self.history_rows.append(
            {
                "id": row_id,
                "alert_id": alert_id,
                "event": event,
                "area": area,
                "disposition": disposition,
                "transmitted_text": transmitted_text,
                "detail": detail,
                "transmit_status": transmit_status,
                "revision_hash": revision_hash,
                "metadata": metadata or {},
            }
        )
        return row_id

    def refresh_dry_run_history_text(self, history_id, transmitted_text):
        for row in self.history_rows:
            if row["id"] == history_id and row["transmit_status"] == "dry-run":
                row["transmitted_text"] = transmitted_text
                return

    def update_history_transmit_status(self, history_id, transmit_status, detail=None):
        for row in self.history_rows:
            if row["id"] == history_id:
                row["transmit_status"] = transmit_status
                if detail is not None:
                    row["detail"] = detail
                return

    def add_event(self, level, message):
        self.events.append((level, message))

    def add_error(self, source, message):
        self.errors.append((source, message))

    def upsert_state(self, **kwargs):
        self.state_rows.append(kwargs)


class _FakeTx:
    message_budget = 195
    def __init__(self):
        self.enqueued = []

    def enqueue_verification(self, text, on_result=None, allow_new=True):
        pending = next((x for x in self.enqueued if x["text"] == text), None)
        if pending:
            self.enqueued.remove(pending)
            self.enqueued.append(pending)
            return True
        if not allow_new:
            return False
        self.enqueue(text, on_result=on_result)
        return True

    def enqueue(self, text, channel=None, on_result=None, delay_after=None):
        self.enqueued.append(
            {
                "text": text,
                "channel": channel,
                "on_result": on_result,
                "delay_after": delay_after,
            }
        )
        return True


def _warning_item(alert_id: str = "abc"):
    return {
        "id": alert_id,
        "event": "Severe Thunderstorm Warning",
        "headline": "warning",
        "area_desc": "Illawarra",
        "effective": "2026-09-28T08:00:00+00:00",
        "expires": TEST_EXPIRY,
        "message_type": "Alert",
    }


@pytest.mark.asyncio
async def test_poller_queues_parts_then_final_verification_with_expected_delays(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])

    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["1/2 BOM NEW Severe Thunderstorm Warning: first", "2/2 second; check bom.gov.au"])

    await poller._process(_warning_item(), rules, "Australia/Sydney", 0, dry_run=False)

    assert len(tx.enqueued) == 2
    assert "BOM NEW" in tx.enqueued[0]["text"]
    assert "Severe Thunderstorm Warning" in tx.enqueued[0]["text"]
    assert "BOM NEW" not in tx.enqueued[1]["text"]
    assert "1/2" in tx.enqueued[0]["text"] and "first" in tx.enqueued[0]["text"]
    assert "2/2" in tx.enqueued[1]["text"] and tx.enqueued[1]["text"].endswith("check bom.gov.au")
    assert tx.enqueued[0]["delay_after"] == MULTIPART_GAP_SECONDS
    assert tx.enqueued[1]["delay_after"] == BURST_GAP_SECONDS
    assert len(FINAL_VERIFICATION_MESSAGE.encode("utf-8")) <= 195


@pytest.mark.asyncio
async def test_poller_cancellation_also_queues_final_verification(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])

    class _Decision:
        disposition = "cancelled"
        transmit = True
        detail = "early cancellation"

    monkeypatch.setattr("app.poller.decide", lambda alert, rules, get_state: _Decision())

    item = _warning_item("cancel-1")
    item["message_type"] = "Cancel"
    item["event"] = "Cancellation of Severe Thunderstorm Warning"

    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=False)

    assert len(tx.enqueued) == 1
    assert tx.enqueued[0]["text"].startswith("BOM CANCELLED Severe Thunderstorm Warning")
    assert "Cancellation of" not in tx.enqueued[0]["text"]
    assert tx.enqueued[0]["text"].endswith("; check bom.gov.au")
    assert tx.enqueued[0]["delay_after"] == BURST_GAP_SECONDS


@pytest.mark.asyncio
async def test_poller_records_state_only_after_all_multipart_parts_succeed(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])

    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["1/2 BOM NEW Severe Thunderstorm Warning: first", "2/2 second; check bom.gov.au"])

    await poller._process(_warning_item(), rules, "Australia/Sydney", 0, dry_run=False)

    assert len(tx.enqueued) == 2
    assert db.state_rows == []
    assert db.history_rows[0]["transmit_status"] == "queued"

    tx.enqueued[0]["on_result"](True, "")
    assert db.state_rows == []

    tx.enqueued[1]["on_result"](True, "")
    assert len(db.state_rows) == 1
    assert db.history_rows[0]["transmit_status"] == "success"


@pytest.mark.asyncio
async def test_poller_does_not_record_state_when_any_multipart_part_fails(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])

    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["1/2 BOM NEW Severe Thunderstorm Warning: first", "2/2 second; check bom.gov.au"])

    item = _warning_item("def")
    item["area_desc"] = "Hunter"

    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=False)

    tx.enqueued[0]["on_result"](True, "")
    tx.enqueued[1]["on_result"](False, "link down")

    assert db.state_rows == []
    assert db.history_rows[0]["transmit_status"] == "failed"
    assert "broadcast failed: link down" in db.history_rows[0]["detail"]
    assert poller.status.last_broadcast_failure is not None
    assert "NOT SENT on MeshCore" in db.errors[-1][1]
    assert "1/2 BOM NEW" in db.errors[-1][1]


@pytest.mark.asyncio
async def test_poller_dry_run_logs_history_and_events_with_final_verification(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])

    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["1/2 BOM NEW Severe Thunderstorm Warning: first", "2/2 second; check bom.gov.au"])

    await poller._process(_warning_item("dry-1"), rules, "Australia/Sydney", 0, dry_run=True)

    assert tx.enqueued == []
    assert len(db.events) == 2
    assert db.events[0][1].startswith("[DRY-RUN] would send: 1/2 BOM NEW")
    assert "1/2 BOM NEW" in db.events[0][1]
    assert "2/2 second" in db.events[1][1]
    assert db.events[1][1].endswith("check bom.gov.au")
    assert len(db.history_rows) == 1
    assert db.history_rows[0]["transmitted_text"] == " || ".join(
        event.removeprefix("[DRY-RUN] would send: ") for _, event in db.events)
    assert db.history_rows[0]["detail"].startswith("DRY-RUN:")
    assert db.history_rows[0]["transmit_status"] == "dry-run"
    assert db.state_rows == []


@pytest.mark.asyncio
async def test_dry_run_alert_is_queued_when_broadcasting_goes_live(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["warning"])
    item = _warning_item("dry-to-live")

    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=True)
    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=False)

    assert len(db.history_rows) == 1
    assert db.history_rows[0]["transmit_status"] == "queued"
    assert db.history_rows[0]["detail"] == "new alert"
    assert len(tx.enqueued) == 1


@pytest.mark.asyncio
async def test_history_records_changed_warning_body_once_per_revision(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["warning"])
    item = _warning_item("same-link")
    item["detail"] = "Initial warning details"

    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=True)
    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=True)
    assert len(db.history_rows) == 1

    revised = {**item, "detail": "Changed warning details"}
    await poller._process(revised, rules, "Australia/Sydney", 0, dry_run=True)
    await poller._process(revised, rules, "Australia/Sydney", 0, dry_run=True)
    assert len(db.history_rows) == 2
    assert db.history_rows[0]["disposition"] == "sent"
    assert db.history_rows[1]["disposition"] == "update"


@pytest.mark.asyncio
async def test_delivery_callback_updates_only_its_history_revision(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["warning"])
    first = _warning_item("same-link")
    second = {**first, "detail": "Revised content"}

    await poller._process(first, rules, "Australia/Sydney", 0, dry_run=False)
    first_callback = tx.enqueued[-1]["on_result"]
    await poller._process(second, rules, "Australia/Sydney", 0, dry_run=False)
    second_callback = tx.enqueued[-1]["on_result"]
    second_callback(True, "")
    first_callback(False, "old send failed")

    assert len(db.history_rows) == 2
    assert db.history_rows[0]["transmit_status"] == "failed"
    assert db.history_rows[1]["transmit_status"] == "success"


@pytest.mark.asyncio
async def test_marine_api_area_change_is_new_revision_without_rss_change(monkeypatch):
    from app.bom_enricher import BOMEnrichment, WarningSection

    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    item = {
        **_warning_item("http://reg.bom.gov.au/nsw/warnings/marinewind.shtml"),
        "event": "Marine Wind Warning", "area_desc": "New South Wales",
        "headline": "Marine Wind Warning Summary for New South Wales",
        "references": ["http://reg.bom.gov.au/nsw/warnings/marinewind.shtml"],
    }
    details = [
        BOMEnrichment(sections=(WarningSection("Strong Wind Warning", "Hunter Coast", "REN"),)),
        BOMEnrichment(sections=(WarningSection("Strong Wind Warning", "Hunter Coast", "REN"),)),
        BOMEnrichment(sections=(
            WarningSection("Strong Wind Warning", "Hunter Coast, Sydney Coast", "REN"),
            WarningSection("Cancellation", "Batemans Coast and Eden Coast", "CAN"),
        )),
    ]

    async def enrich(url):
        return details.pop(0)

    monkeypatch.setattr(poller._enricher, "enrich", enrich)
    for _ in range(3):
        await poller._process(item, rules, "Australia/Sydney", 0, dry_run=True)

    assert len(db.history_rows) == 2
    assert db.history_rows[0]["revision_hash"] != db.history_rows[1]["revision_hash"]
    assert "Sydney Coast" in db.history_rows[1]["transmitted_text"]
    assert "CANCELLED for Batemans Coast and Eden Coast" in db.history_rows[1]["transmitted_text"]
    assert db.history_rows[1]["transmitted_text"].count("Marine Wind Warning") == 1
    assert "Batemans Coast" in db.history_rows[1]["transmitted_text"]
    assert tx.enqueued == []


@pytest.mark.asyncio
async def test_existing_dry_run_revision_refreshes_prepared_wording(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    item = _warning_item("same-revision")
    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["Wed: warning"])
    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=True)
    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["Wednesday: warning"])
    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=True)

    assert len(db.history_rows) == 2
    assert "Wed: warning" in db.history_rows[0]["transmitted_text"]
    assert "Wednesday: warning" in db.history_rows[1]["transmitted_text"]


def test_verification_success_starts_persistent_five_minute_cooldown():
    db = _FakeDb()
    tx = _FakeTx()
    BomPoller(db, tx)._queue_verification(0, dry_run=False)
    assert [item["text"] for item in tx.enqueued] == [FINAL_VERIFICATION_MESSAGE]
    tx.enqueued.pop()["on_result"](True, "")

    restarted = BomPoller(db, tx)
    restarted._queue_verification(0, dry_run=False)
    assert tx.enqueued == []
    assert db.get_setting("verification_live_last_ts")["0"]


def test_verification_failure_does_not_start_cooldown():
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    poller._queue_verification(0, dry_run=False)
    tx.enqueued.pop()["on_result"](False, "radio unavailable")
    assert db.get_setting("verification_live_last_ts") is None
    poller._queue_verification(0, dry_run=False)
    assert [item["text"] for item in tx.enqueued] == [FINAL_VERIFICATION_MESSAGE]
    assert "verification message failed" in db.errors[-1][1]


def test_dry_run_verification_appears_once_in_five_minutes():
    db = _FakeDb()
    poller = BomPoller(db, _FakeTx())
    poller._queue_verification(0, dry_run=True)
    poller._queue_verification(0, dry_run=True)
    assert [message for _, message in db.events] == [
        f"[DRY-RUN] would send: {FINAL_VERIFICATION_MESSAGE}"]
    assert db.get_setting("verification_dry_run_last_ts")["0"]


def test_verification_is_due_again_after_five_minutes():
    from datetime import datetime, timedelta, timezone

    db = _FakeDb()
    old = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    db.set_setting("verification_live_last_ts", {"0": old})
    tx = _FakeTx()
    BomPoller(db, tx)._queue_verification(0, dry_run=False)
    assert [item["text"] for item in tx.enqueued] == [FINAL_VERIFICATION_MESSAGE]


@pytest.mark.asyncio
async def test_verification_failure_does_not_change_successful_warning_history(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["warning"])
    await poller._process(_warning_item("verified-warning"), rules,
                          "Australia/Sydney", 0, dry_run=False)
    poller._queue_verification(0, dry_run=False)
    tx.enqueued[0]["on_result"](True, "")
    tx.enqueued[1]["on_result"](False, "radio unavailable")
    assert db.history_rows[0]["transmit_status"] == "success"
    assert len(db.state_rows) == 1
    assert "verification message failed" in db.errors[-1][1]


@pytest.mark.asyncio
async def test_bom_source_note_fits_multipart_radio_budget():
    db = _FakeDb()
    tx = _FakeTx()
    tx.message_budget = 80
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    item = _warning_item("source-budget")
    item["area_desc"] = "Hunter, Sydney, Illawarra, South Coast and Central Tablelands"
    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=False)
    parts = [entry["text"] for entry in tx.enqueued]
    assert len(parts) >= 2
    assert parts[-1].endswith("check bom.gov.au")
    assert all(len(part.encode("utf-8")) <= tx.message_budget for part in parts)


@pytest.mark.asyncio
async def test_bom_poll_does_not_queue_same_revision_twice_while_pending(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    item = _warning_item()
    await poller._process(item.copy(), rules, "Australia/Sydney", 0, dry_run=False)
    await poller._process(item.copy(), rules, "Australia/Sydney", 0, dry_run=False)
    assert len(tx.enqueued) == 1
    assert len(db.history_rows) == 1
