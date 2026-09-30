import pytest

from app.config import BURST_GAP_SECONDS, FINAL_VERIFICATION_MESSAGE, MULTIPART_GAP_SECONDS
from app.filters import FilterRules
from app.poller import BomPoller


class _FakeDb:
    def __init__(self):
        self.state_rows = []
        self.history_rows = []
        self.events = []
        self.errors = []

    def get_state(self, alert_id):
        return None

    def latest_history(self, alert_id):
        for row in reversed(self.history_rows):
            if row["alert_id"] == alert_id:
                return row
        return None

    def add_history(self, alert_id, event, area, disposition, transmitted_text="", detail="",
                    transmit_status=None, revision_hash=""):
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
            }
        )
        return row_id

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
    def __init__(self):
        self.enqueued = []

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
        "expires": "2026-09-28T10:00:00+00:00",
        "message_type": "Alert",
    }


@pytest.mark.asyncio
async def test_poller_queues_parts_then_final_verification_with_expected_delays(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])

    monkeypatch.setattr("app.poller.build_mesh_parts", lambda alert, tz: ["1/2 first", "2/2 second"])

    await poller._process(_warning_item(), rules, "Australia/Sydney", 0, dry_run=False)

    assert [m["text"] for m in tx.enqueued] == [
        "1/2 first",
        "2/2 second",
        FINAL_VERIFICATION_MESSAGE,
    ]
    assert tx.enqueued[0]["delay_after"] == MULTIPART_GAP_SECONDS
    assert tx.enqueued[1]["delay_after"] == MULTIPART_GAP_SECONDS
    assert tx.enqueued[2]["delay_after"] == BURST_GAP_SECONDS
    assert tx.enqueued[2]["text"] == FINAL_VERIFICATION_MESSAGE
    assert not tx.enqueued[2]["text"].startswith("1/2")
    assert "1/" not in tx.enqueued[2]["text"]
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

    assert len(tx.enqueued) == 2
    assert tx.enqueued[0]["text"].startswith("CANCELLED:")
    assert tx.enqueued[-1]["text"] == FINAL_VERIFICATION_MESSAGE
    assert tx.enqueued[0]["delay_after"] == MULTIPART_GAP_SECONDS
    assert tx.enqueued[1]["delay_after"] == BURST_GAP_SECONDS


@pytest.mark.asyncio
async def test_poller_records_state_only_after_all_multipart_parts_succeed(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])

    monkeypatch.setattr("app.poller.build_mesh_parts", lambda alert, tz: ["1/2 first", "2/2 second"])

    await poller._process(_warning_item(), rules, "Australia/Sydney", 0, dry_run=False)

    assert len(tx.enqueued) == 3
    assert db.state_rows == []
    assert db.history_rows[0]["transmit_status"] == "queued"

    tx.enqueued[0]["on_result"](True, "")
    assert db.state_rows == []

    tx.enqueued[1]["on_result"](True, "")
    assert db.state_rows == []

    tx.enqueued[2]["on_result"](True, "")
    assert len(db.state_rows) == 1
    assert db.history_rows[0]["transmit_status"] == "success"


@pytest.mark.asyncio
async def test_poller_does_not_record_state_when_any_multipart_part_fails(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])

    monkeypatch.setattr("app.poller.build_mesh_parts", lambda alert, tz: ["1/2 first", "2/2 second"])

    item = _warning_item("def")
    item["area_desc"] = "Hunter"

    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=False)

    tx.enqueued[0]["on_result"](True, "")
    tx.enqueued[1]["on_result"](True, "")
    tx.enqueued[2]["on_result"](False, "link down")

    assert db.state_rows == []
    assert db.history_rows[0]["transmit_status"] == "failed"
    assert "broadcast failed: link down" in db.history_rows[0]["detail"]
    assert poller.status.last_broadcast_failure is not None
    assert "NOT SENT on MeshCore" in db.errors[-1][1]
    assert FINAL_VERIFICATION_MESSAGE in db.errors[-1][1]


@pytest.mark.asyncio
async def test_poller_dry_run_logs_history_and_events_with_final_verification(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])

    monkeypatch.setattr("app.poller.build_mesh_parts", lambda alert, tz: ["1/2 first", "2/2 second"])

    await poller._process(_warning_item("dry-1"), rules, "Australia/Sydney", 0, dry_run=True)

    assert tx.enqueued == []
    assert len(db.events) == 3
    assert db.events[0][1] == "[DRY-RUN] would send: 1/2 first"
    assert db.events[1][1] == "[DRY-RUN] would send: 2/2 second"
    assert db.events[2][1] == f"[DRY-RUN] would send: {FINAL_VERIFICATION_MESSAGE}"
    assert len(db.history_rows) == 1
    assert db.history_rows[0]["transmitted_text"] == (
        f"1/2 first || 2/2 second || {FINAL_VERIFICATION_MESSAGE}"
    )
    assert db.history_rows[0]["detail"].startswith("DRY-RUN:")
    assert db.history_rows[0]["transmit_status"] == "dry-run"
    assert db.state_rows == []


@pytest.mark.asyncio
async def test_dry_run_alert_is_queued_when_broadcasting_goes_live(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    monkeypatch.setattr("app.poller.build_mesh_parts", lambda alert, tz: ["warning"])
    item = _warning_item("dry-to-live")

    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=True)
    await poller._process(item, rules, "Australia/Sydney", 0, dry_run=False)

    assert len(db.history_rows) == 1
    assert db.history_rows[0]["transmit_status"] == "queued"
    assert db.history_rows[0]["detail"] == "new alert"
    assert len(tx.enqueued) == 2


@pytest.mark.asyncio
async def test_history_records_changed_warning_body_once_per_revision(monkeypatch):
    db = _FakeDb()
    tx = _FakeTx()
    poller = BomPoller(db, tx)
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    monkeypatch.setattr("app.poller.build_mesh_parts", lambda alert, tz: ["warning"])
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
    monkeypatch.setattr("app.poller.build_mesh_parts", lambda alert, tz: ["warning"])
    first = _warning_item("same-link")
    second = {**first, "detail": "Revised content"}

    await poller._process(first, rules, "Australia/Sydney", 0, dry_run=False)
    first_callback = tx.enqueued[-1]["on_result"]
    await poller._process(second, rules, "Australia/Sydney", 0, dry_run=False)
    second_callback = tx.enqueued[-1]["on_result"]
    second_callback(True, "")
    second_callback(True, "")
    first_callback(False, "old send failed")
    first_callback(True, "")

    assert len(db.history_rows) == 2
    assert db.history_rows[0]["transmit_status"] == "failed"
    assert db.history_rows[1]["transmit_status"] == "success"
