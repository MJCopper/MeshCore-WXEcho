import asyncio

import pytest

from app.config import BURST_GAP_SECONDS, QUEUE_MAX
from app.transmit import TransmitManager


class _FakeDb:
    def get_setting(self, key, default=None):
        return default

    def add_event(self, level, message):
        return None

    def add_error(self, source, message):
        return None

    def add_transmit_log(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_worker_uses_item_delay_after_without_stacking_burst_gap(monkeypatch):
    tx = TransmitManager(_FakeDb())
    calls = []
    sleeps = []

    async def fake_transmit(item):
        calls.append(item.text)
        if len(calls) == 2:
            tx._stopped = True
            tx._queue_event.set()
        return True, ""

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(tx, "_transmit_item", fake_transmit)
    monkeypatch.setattr("app.transmit.asyncio.sleep", fake_sleep)

    tx.enqueue("part-1", delay_after=3)
    tx.enqueue("part-2")

    await asyncio.wait_for(tx._worker(), timeout=1)

    assert calls == ["part-1", "part-2"]
    assert sleeps and sleeps[0] == 3


@pytest.mark.asyncio
async def test_worker_uses_default_burst_gap_for_normal_items(monkeypatch):
    tx = TransmitManager(_FakeDb())
    calls = []
    sleeps = []

    async def fake_transmit(item):
        calls.append(item.text)
        if len(calls) == 2:
            tx._stopped = True
            tx._queue_event.set()
        return True, ""

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(tx, "_transmit_item", fake_transmit)
    monkeypatch.setattr("app.transmit.asyncio.sleep", fake_sleep)

    tx.enqueue("normal-1")
    tx.enqueue("normal-2")

    await asyncio.wait_for(tx._worker(), timeout=1)

    assert calls == ["normal-1", "normal-2"]
    assert sleeps and sleeps[0] == BURST_GAP_SECONDS


@pytest.mark.asyncio
async def test_idle_radio_reconnects_after_being_unavailable(monkeypatch):
    db = _FakeDb()
    tx = TransmitManager(db)
    transport = tx._transports["meshcore"]
    transport.target = "/dev/serial/by-id/usb-meshcore"
    attempts = []
    sleeps = []

    async def fake_ensure(current):
        attempts.append(current.target)
        current.connected = len(attempts) > 1
        if current.connected:
            current.tx = type("Radio", (), {"connected": True})()
        return current.connected

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            tx._stopped = True

    monkeypatch.setattr(tx, "_ensure", fake_ensure)
    monkeypatch.setattr("app.transmit.asyncio.sleep", fake_sleep)
    await asyncio.wait_for(tx._maintain_connections(), timeout=1)

    assert attempts == [transport.target, transport.target]
    assert sleeps[:2] == [15, 15]


@pytest.mark.asyncio
async def test_idle_radio_detects_disconnection(monkeypatch):
    tx = TransmitManager(_FakeDb())
    transport = tx._transports["meshcore"]
    transport.target = "/dev/serial/by-id/usb-meshcore"
    transport.connected = True
    transport.tx = type("Radio", (), {"connected": False})()
    assert not tx.connected
    assert not tx.status()[0]["connected"]
    recovered = []

    async def fake_reconnect(current):
        recovered.append(current.target)
        tx._stopped = True
        return True

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(tx, "_reconnect", fake_reconnect)
    monkeypatch.setattr("app.transmit.asyncio.sleep", fake_sleep)
    await asyncio.wait_for(tx._maintain_connections(), timeout=1)

    assert recovered == [transport.target]


def test_verification_stays_once_at_tail_when_more_warnings_arrive():
    tx = TransmitManager(_FakeDb())
    tx.enqueue("warning-1")
    assert tx.enqueue_verification("verification") is True
    tx.enqueue("warning-2")
    assert tx.enqueue_verification("verification") is True
    assert [item.text for item in tx._queue] == ["warning-1", "warning-2", "verification"]
    assert sum(item.verification for item in tx._queue) == 1


def test_verification_has_reserved_place_after_full_warning_queue():
    tx = TransmitManager(_FakeDb())
    warning_limit = tx._queue.maxlen - 1
    for number in range(warning_limit):
        tx.enqueue(f"warning-{number}")
    assert tx.enqueue_verification("verification") is True
    assert len(tx._queue) == warning_limit + 1
    assert tx.enqueue("new-warning") is False
    assert len(tx._queue) == warning_limit + 1
    assert tx._queue[-1].text == "verification"
    assert tx._queue[0].text == "warning-0"
    assert tx._queue[-2].text == "warning-19"


def test_notice_admission_is_atomic_and_never_evicts_prior_parts():
    tx = TransmitManager(_FakeDb())
    for index in range(QUEUE_MAX):
        assert tx.enqueue(f"older-{index}")
    before = [item.text for item in tx._queue]
    assert not tx.enqueue_notice([("1/2 newer", 3), ("2/2 newer", 30)])
    assert [item.text for item in tx._queue] == before
    tx._next_queued_part()
    assert tx.enqueue_notice([("one-part", 30)])
    assert tx.enqueue_verification("verification")
    assert [item.text for item in tx._queue][-2:] == ["one-part", "verification"]


def test_priority_keeps_parts_together_and_verification_last():
    tx = TransmitManager(_FakeDb())
    assert tx.enqueue_notice([("traffic-a", 3), ("traffic-b", 30)], priority=5)
    assert tx.enqueue_verification("verification")
    assert tx.enqueue_notice([("bom-a", 3), ("bom-b", 30)], priority=1)
    assert tx.enqueue_notice([("rfs-emergency", 30)], priority=0)
    emitted = []
    while tx._queue:
        emitted.append(tx._next_queued_part().text)
    assert emitted == [
        "rfs-emergency", "bom-a", "bom-b", "traffic-a", "traffic-b", "verification"]


def test_new_priority_notice_waits_for_active_multipart_notice():
    tx = TransmitManager(_FakeDb())
    assert tx.enqueue_notice([("traffic-a", 3), ("traffic-b", 30)], priority=5)
    transmitting = tx._next_queued_part()
    tx._active_notice = transmitting.notice_id
    assert tx.enqueue_notice([("rfs-emergency", 30)], priority=0)
    assert [item.text for item in tx._queue] == ["traffic-b", "rfs-emergency"]
