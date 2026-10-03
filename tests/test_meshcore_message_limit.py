"""MeshCore must never accept a command the firmware would shorten."""
from types import SimpleNamespace

import pytest

from app.config import MAX_PAYLOAD_BYTES
from app.formatter import _split_complete_message, append_source_note
from app.transmit import MeshCoreTransmitter, TxUnsent


@pytest.mark.asyncio
async def test_sender_name_budget_blocks_oversized_send_before_radio_call():
    radio = MeshCoreTransmitter("serial", "/dev/null")
    called = []

    async def send_chan_msg(channel, text):
        called.append((channel, text))

    radio._mc = SimpleNamespace(commands=SimpleNamespace(send_chan_msg=send_chan_msg))
    radio._sender_name = "WXEcho"
    assert radio.message_budget == 152
    with pytest.raises(TxUnsent, match="MeshCore allows 152"):
        await radio.send_text("x" * 153, 0)
    assert called == []
    assert MAX_PAYLOAD_BYTES == 126


def test_split_preserves_unicode_text_under_sender_budget():
    message = "Warning for Hunter Coast, Sydney Coast and Illawarra Coast: " + "暴風 " * 30
    parts = _split_complete_message(message, 126)
    assert len(parts) > 1
    assert all(len(part.encode("utf-8")) <= 126 for part in parts)
    reconstructed = " ".join(part.split(" ", 1)[1] for part in parts)
    assert reconstructed == " ".join(message.split())


def test_oversized_verification_is_reported_without_queueing():
    from app.config import FINAL_VERIFICATION_MESSAGE
    from app.poller import BomPoller

    class Db:
        def __init__(self):
            self.errors = []
            self.events = []

        def add_error(self, source, detail):
            self.errors.append((source, detail))

        def add_event(self, level, detail):
            self.events.append((level, detail))

    class Tx:
        message_budget = 100

        def enqueue_verification(self, *args, **kwargs):
            raise AssertionError("oversized verification must not be queued")

    db = Db()
    poller = BomPoller(db, Tx())
    poller._queue_verification(0, dry_run=False)
    poller._queue_verification(0, dry_run=False)
    assert len(db.errors) == 1
    assert len(FINAL_VERIFICATION_MESSAGE.encode()) <= 126
    assert len(FINAL_VERIFICATION_MESSAGE.encode()) > 100
    assert db.errors and "exceeds MeshCore limit" in db.errors[0][1]


def test_source_note_moves_whole_to_next_part_when_last_part_is_full():
    parts = append_source_note(["A" * 30], "; check rfs.nsw.gov.au", 30)
    assert parts == ["A" * 30, "check rfs.nsw.gov.au"]
    assert all(len(part.encode("utf-8")) <= 30 for part in parts)

def test_source_note_stays_with_last_part_when_it_fits():
    assert append_source_note(["warning"], "; check bom.gov.au", 30) == ["warning; check bom.gov.au"]


@pytest.mark.asyncio
async def test_unreadable_local_tx_counter_is_not_reported_as_success(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "meshcore", SimpleNamespace(EventType=SimpleNamespace(ERROR="error")))
    radio = MeshCoreTransmitter("serial", "/dev/null")
    radio._sender_name = "WXEcho"
    calls = []

    async def send_chan_msg(channel, text):
        calls.append((channel, text))
        return SimpleNamespace(type="ok")

    async def unreadable_counter():
        return None

    radio._mc = SimpleNamespace(commands=SimpleNamespace(send_chan_msg=send_chan_msg))
    monkeypatch.setattr(radio, "_flood_tx", unreadable_counter)
    with pytest.raises(TxUnsent) as raised:
        await radio.send_text("warning", 0)
    assert raised.value.category == "unverified"
    assert calls == [(0, "warning")]
