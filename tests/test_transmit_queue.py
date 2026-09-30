import asyncio

import pytest

from app.config import BURST_GAP_SECONDS
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
    tx.enqueue("new-warning")
    assert len(tx._queue) == warning_limit + 1
    assert tx._queue[-1].text == "verification"
    assert tx._queue[0].text == "warning-1"
    assert tx._queue[-2].text == "new-warning"
