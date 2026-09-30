"""MeshCore must never accept a command the firmware would shorten."""
from types import SimpleNamespace

import pytest

from app.config import MAX_PAYLOAD_BYTES
from app.formatter import _split_complete_message
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
        message_budget = 126

        def enqueue_verification(self, *args, **kwargs):
            raise AssertionError("oversized verification must not be queued")

    db = Db()
    poller = BomPoller(db, Tx())
    poller._queue_verification(0, dry_run=False)
    assert len(FINAL_VERIFICATION_MESSAGE.encode()) == 134
    assert len(FINAL_VERIFICATION_MESSAGE.encode()) > 126
    assert db.errors and "exceeds MeshCore limit" in db.errors[0][1]
