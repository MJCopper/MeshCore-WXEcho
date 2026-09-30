"""MeshCore transmit layer.

Each protocol is an independent transport with its own enable flag and
connection (serial or TCP). TransmitManager fans every outbound message out to
ALL enabled transports, paces bursts, and manages per-transport reconnects.
Dry-run lives in the poller; manual sends bypass pacing. Backends are imported
lazily so a missing optional dependency (e.g. meshcore) never breaks startup.
"""
from __future__ import annotations

import abc
import asyncio
import logging
import math
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .config import BURST_GAP_SECONDS, REPEAT_GAP_SECONDS, QUEUE_MAX, MAX_PAYLOAD_BYTES


class TxUnsent(Exception):
    """Raised when a radio did not actually transmit a message. `category` tells
    the sender WHY, so it can apply the right correction instead of blindly
    retrying:
      'duty_cycle' - radio hit its airtime/duty-cycle cap: wait, then retry
      'queue_full' - the radio's TX queue is full: wait for it to drain, retry
      'too_large'  - message exceeds the radio payload: shrink it (unrecoverable here)
      'no_channel' - the configured channel index does not exist: config error
      'link'       - radio interface down / no response: reconnect, then retry
      'unsent'     - radio accepted the command but did not key up: wait/reconnect
    """
    def __init__(self, category: str, detail: str):
        super().__init__(detail)
        self.category = category
        self.detail = detail

logger = logging.getLogger("wx_echo.tx")


class Transmitter(abc.ABC):
    label = "?"

    @abc.abstractmethod
    async def connect(self) -> None: ...
    @abc.abstractmethod
    async def send_text(self, text: str, channel: int) -> None: ...
    @abc.abstractmethod
    async def close(self) -> None: ...
    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    async def read_channels(self) -> list:
        """Return the channels configured on the device: [{index, name}]. Optional."""
        return []


class MeshCoreTransmitter(Transmitter):
    label = "MeshCore"

    def __init__(self, conn: str, port: str = "", host: str = "", baud: int = 115200):
        self.conn, self.port, self.host, self.baud = conn, port, host, baud
        self._mc = None

    async def connect(self) -> None:
        from meshcore import MeshCore  # lazy: optional dependency
        if self._mc is not None:
            await self.close()
        # default_timeout: cap how long we wait for the device's "OK" confirmation
        #   (channel broadcasts have no real ACK, so a long wait just stalls sends).
        # auto_reconnect: let the library recover a dropped USB/TCP link on its own.
        if self.conn == "tcp":
            h, _, p = self.host.partition(":")
            self._mc = await MeshCore.create_tcp(
                h, int(p or 4000), default_timeout=6.0, auto_reconnect=True)
        else:
            self._mc = await MeshCore.create_serial(
                self.port, self.baud, default_timeout=6.0, auto_reconnect=True)
        if self._mc is None:
            where = self.host if self.conn == "tcp" else self.port
            raise RuntimeError("no response from MeshCore node on %s" % (where or "(unset)"))

    async def send_text(self, text: str, channel: int) -> None:
        if self._mc is None:
            raise RuntimeError("not connected")
        # The device's OK/timeout is NOT proof of RF -- a channel broadcast has no
        # ACK, so an "OK" only means the command was accepted, not that the radio
        # keyed up. Confirm the actual transmission by watching the radio's own
        # flood-TX counter advance (proven: it ticks by 1 per real send, and stays
        # flat when the radio does not transmit).
        from meshcore import EventType
        before = await self._flood_tx()
        res = await self._mc.commands.send_chan_msg(channel, text)
        # Capture the device's own error reason, if it gave one, for diagnostics.
        reason = ""
        if getattr(res, "type", None) == EventType.ERROR:
            payload = getattr(res, "payload", {}) or {}
            reason = payload.get("reason", "") if isinstance(payload, dict) else ""
        if before is None:
            return  # counter unreadable: fall back to best-effort (never block sends)
        for _ in range(15):                       # poll up to ~4.5s
            await asyncio.sleep(0.3)
            after = await self._flood_tx()
            if after is not None and after > before:
                return                            # verified on the air (flood_tx advanced)
        detail = "radio did not transmit (TX counter did not advance%s)" % (
            "; %s" % reason if reason else "")
        raise TxUnsent("unsent", detail)

    async def _flood_tx(self):
        try:
            res = await self._mc.commands.get_stats_packets()
            p = getattr(res, "payload", {}) or {}
            v = p.get("flood_tx")
            return int(v) if v is not None else None
        except Exception:
            return None

    async def close(self) -> None:
        if self._mc is not None:
            mc, self._mc = self._mc, None
            try:
                await mc.disconnect()
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return self._mc is not None and self._mc.is_connected

    async def read_channels(self) -> list:
        if self._mc is None:
            raise RuntimeError("not connected")
        from meshcore import EventType
        out = []
        for idx in range(0, 8):
            res = await self._mc.commands.get_channel(idx)
            if getattr(res, "type", None) != EventType.CHANNEL_INFO:
                break  # device ran out of channel slots
            p = getattr(res, "payload", {}) or {}
            name = (p.get("channel_name") or "").strip()
            if not name:
                continue  # empty/unconfigured slot
            out.append({"index": int(p.get("channel_idx", idx)), "name": name})
        return out

    async def read_info(self) -> dict:
        if self._mc is None:
            return {}
        from meshcore import EventType
        try:
            res = await self._mc.commands.send_device_query()
        except Exception:
            return {}
        if getattr(res, "type", None) != EventType.DEVICE_INFO:
            return {}
        p = getattr(res, "payload", {}) or {}
        return {"model": (p.get("model") or "").strip(),
                "firmware": (p.get("ver") or "").strip()}

    async def read_settings(self) -> dict:
        from meshcore import EventType

        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        info = await self._mc.commands.send_device_query()
        if info.type != EventType.DEVICE_INFO:
            raise RuntimeError("could not read MeshCore device info")
        self_info = await self._mc.commands.send_appstart()
        if self_info.type != EventType.SELF_INFO:
            raise RuntimeError("could not read MeshCore settings")

        device = info.payload or {}
        settings = self_info.payload or {}
        battery_mv = None
        try:
            battery = await self._mc.commands.get_bat()
            if battery.type == EventType.BATTERY:
                battery_mv = (battery.payload or {}).get("level")
        except Exception:
            pass
        channels = []
        available_slots = []
        max_channels = min(8, max(1, int(device.get("max_channels") or 8)))
        for index in range(max_channels):
            result = await self._mc.commands.get_channel(index)
            if result.type != EventType.CHANNEL_INFO:
                break
            payload = result.payload or {}
            name = (payload.get("channel_name") or "").strip()
            if name:
                channels.append({"index": index, "name": name,
                                 "hash": payload.get("channel_hash") or ""})
            elif index > 0 and payload.get("channel_secret") == bytes(16):
                available_slots.append(index)
        return {
            "name": (settings.get("name") or "").strip(),
            "model": (device.get("model") or "").strip(),
            "firmware": (device.get("ver") or "").strip(),
            "battery_mv": battery_mv,
            "tx_power": settings.get("tx_power"),
            "max_tx_power": settings.get("max_tx_power"),
            "radio_freq": settings.get("radio_freq"),
            "radio_bw": settings.get("radio_bw"),
            "radio_sf": settings.get("radio_sf"),
            "radio_cr": settings.get("radio_cr"),
            "channels": channels,
            "available_slots": available_slots,
            "max_channels": max_channels,
        }

    async def set_device_name(self, name: str) -> str:
        from meshcore import EventType

        name = name.strip()
        if not name or len(name.encode("utf-8")) > 32 or any(ord(char) < 32 for char in name):
            raise ValueError("device name must be 1-32 UTF-8 bytes without control characters")
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        result = await self._mc.commands.set_name(name)
        if result.type != EventType.OK:
            raise RuntimeError("radio rejected the device name")
        verified = await self._mc.commands.send_appstart()
        if verified.type != EventType.SELF_INFO or (verified.payload or {}).get("name", "").strip() != name:
            raise RuntimeError("could not verify the saved device name")
        return name

    async def set_tx_power(self, power: int) -> int:
        from meshcore import EventType

        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        current = await self._mc.commands.send_appstart()
        if current.type != EventType.SELF_INFO:
            raise RuntimeError("could not read the radio's TX power limit")
        limit = (current.payload or {}).get("max_tx_power")
        if limit is None:
            raise RuntimeError("radio did not report its TX power limit")
        if not 0 <= power <= int(limit):
            raise ValueError(f"TX power must be between 0 and {limit} dBm")
        result = await self._mc.commands.set_tx_power(power)
        if result.type != EventType.OK:
            raise RuntimeError("radio rejected the TX power")
        verified = await self._mc.commands.send_appstart()
        if verified.type != EventType.SELF_INFO or (verified.payload or {}).get("tx_power") != power:
            raise RuntimeError("could not verify the saved TX power")
        return power

    async def set_radio_parameters(self, freq: float, bw: float, sf: int, cr: int) -> dict:
        from meshcore import EventType

        if not (math.isfinite(freq) and 100 <= freq <= 2500):
            raise ValueError("frequency must be between 100 and 2500 MHz")
        if not (math.isfinite(bw) and 1 <= bw <= 2500):
            raise ValueError("bandwidth must be between 1 and 2500 kHz")
        if not 5 <= sf <= 12:
            raise ValueError("spreading factor must be between 5 and 12")
        if not 5 <= cr <= 8:
            raise ValueError("coding rate must be between 5 and 8")
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        result = await self._mc.commands.set_radio(freq, bw, sf, cr)
        if result.type != EventType.OK:
            raise RuntimeError("radio rejected the radio parameters")
        verified = await self._mc.commands.send_appstart()
        if verified.type != EventType.SELF_INFO:
            raise RuntimeError("could not verify the saved radio parameters")
        saved = verified.payload or {}
        try:
            saved_freq = float(saved["radio_freq"])
            saved_bw = float(saved["radio_bw"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("could not verify the saved radio parameters")
        if not (
            math.isclose(saved_freq, freq, rel_tol=0, abs_tol=0.0015) and
            math.isclose(saved_bw, bw, rel_tol=0, abs_tol=0.0015) and
            saved.get("radio_sf") == sf and saved.get("radio_cr") == cr
        ):
            raise RuntimeError("radio parameters did not match after saving")
        return {"radio_freq": saved["radio_freq"], "radio_bw": saved["radio_bw"],
                "radio_sf": sf, "radio_cr": cr}

    async def add_channel(self, name: str, secret_hex: str = "") -> dict:
        from meshcore import EventType

        name = name.strip()
        if not name or name.startswith("#") or len(name.encode("utf-8")) > 32 or any(ord(char) < 32 for char in name):
            raise ValueError("channel name must be 1-32 UTF-8 bytes and cannot start with #")
        if secret_hex.strip():
            supplied = secret_hex.strip()
            if len(supplied) != 32:
                raise ValueError("channel key must be exactly 32 hexadecimal characters")
            try:
                secret = bytes.fromhex(supplied)
            except ValueError as exc:
                raise ValueError("channel key must be exactly 32 hexadecimal characters") from exc
            if secret == bytes(16):
                raise ValueError("channel key cannot be all zeros")
            generated = False
        else:
            secret = secrets.token_bytes(16)
            generated = True
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        device = await self._mc.commands.send_device_query()
        if device.type != EventType.DEVICE_INFO:
            raise RuntimeError("could not read channel capacity")
        max_channels = min(8, max(1, int((device.payload or {}).get("max_channels") or 8)))
        slot = None
        for index in range(1, max_channels):
            current = await self._mc.commands.get_channel(index)
            if current.type != EventType.CHANNEL_INFO:
                continue
            payload = current.payload or {}
            if not (payload.get("channel_name") or "").strip() and payload.get("channel_secret") == bytes(16):
                slot = index
                break
        if slot is None:
            raise RuntimeError("no empty private channel slot is available")
        result = await self._mc.commands.set_channel(slot, name, secret)
        if result.type != EventType.OK:
            raise RuntimeError("radio rejected the new channel")
        verified = await self._mc.commands.get_channel(slot)
        saved = verified.payload or {}
        if (verified.type != EventType.CHANNEL_INFO or saved.get("channel_name") != name
                or saved.get("channel_secret") != secret):
            raise RuntimeError("could not verify the new channel")
        return {"index": slot, "name": name, "secret_hex": secret.hex() if generated else ""}

    async def remove_channel(self, index: int) -> None:
        from meshcore import EventType

        if not 1 <= index < 8:
            raise ValueError("only private channel slots 1-7 can be removed")
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        current = await self._mc.commands.get_channel(index)
        if current.type != EventType.CHANNEL_INFO or not (current.payload or {}).get("channel_name"):
            raise RuntimeError("channel is unavailable")
        result = await self._mc.commands.set_channel(index, "", bytes(16))
        if result.type != EventType.OK:
            raise RuntimeError("radio rejected channel removal")
        verified = await self._mc.commands.get_channel(index)
        saved = verified.payload or {}
        if (verified.type != EventType.CHANNEL_INFO or saved.get("channel_name")
                or saved.get("channel_secret") != bytes(16)):
            raise RuntimeError("could not verify channel removal")

    async def rename_channel(self, index: int, name: str) -> dict:
        from meshcore import EventType

        name = name.strip()
        if not 0 <= index < 8:
            raise ValueError("channel index must be 0-7")
        if not name or name.startswith("#") or len(name.encode("utf-8")) > 32 or any(ord(char) < 32 for char in name):
            raise ValueError("channel name must be 1-32 UTF-8 bytes and cannot start with #")
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")

        current = await self._mc.commands.get_channel(index)
        if current.type != EventType.CHANNEL_INFO:
            raise RuntimeError("channel is unavailable")
        payload = current.payload or {}
        secret = payload.get("channel_secret")
        if not payload.get("channel_name") or not isinstance(secret, bytes) or len(secret) != 16:
            raise RuntimeError("channel cannot be renamed safely")
        result = await self._mc.commands.set_channel(index, name, secret)
        if result.type != EventType.OK:
            raise RuntimeError("radio rejected the channel name")
        verified = await self._mc.commands.get_channel(index)
        updated = verified.payload or {}
        if (verified.type != EventType.CHANNEL_INFO or updated.get("channel_name") != name
                or updated.get("channel_secret") != secret):
            raise RuntimeError("could not verify channel name and key preservation")
        return {"index": index, "name": name, "hash": updated.get("channel_hash") or ""}


def _fmt_model(info: dict) -> str:
    """Human label for a radio from its info dict, e.g. 'Heltec V3 (fw 2.5.9)'."""
    model = (info.get("model") or "").replace("_", " ").strip()
    fw = (info.get("firmware") or "").strip()
    if model and fw:
        return "%s (fw %s)" % (model, fw)
    return model or (("fw %s" % fw) if fw else "")


@dataclass
class Transport:
    name: str                     # "meshcore"
    label: str
    enabled: bool
    conn: str                     # "serial" | "tcp"
    channel: int
    target: str                   # serial path or host - display + "configured?" check
    make: object                  # callable() -> Transmitter
    test_channel: int = 1         # channel used for Troubleshoot tests only
    tx: Transmitter | None = None
    connected: bool = False
    error: str = ""


@dataclass
class QueueItem:
    text: str
    delay_after: float = BURST_GAP_SECONDS
    on_result: object = None   # optional callable(ok: bool, err: str) invoked after the send
    verification: bool = False


def _build_transports(db) -> dict:
    def g(k, d=None):
        return db.get_setting(k, d)

    def num(k, d=0):
        try:
            return int(g(k, d) or d)
        except (TypeError, ValueError):
            return d

    mc_conn = g("meshcore_conn", "serial") or "serial"
    mc = Transport(
        name="meshcore", label="MeshCore",
        enabled=True,
        conn=mc_conn, channel=num("meshcore_channel", 0),
        target=(g("meshcore_host", "") if mc_conn == "tcp" else g("meshcore_port", "")) or "",
        make=lambda: MeshCoreTransmitter(mc_conn, g("meshcore_port", "") or "", g("meshcore_host", "") or ""),
        test_channel=num("meshcore_test_channel", 1),
    )
    return {"meshcore": mc}


class TransmitManager:
    """Serializes node access, paces bursts, fans out to all enabled transports."""

    def __init__(self, db):
        self._db = db
        self._transports = _build_transports(db)
        self._queue: deque[QueueItem] = deque(maxlen=QUEUE_MAX + 1)    # warnings plus one verification
        self._queue_event = asyncio.Event()
        self._verification_in_flight = False
        self._lock = asyncio.Lock()
        self._worker_task: asyncio.Task | None = None
        self._connection_task: asyncio.Task | None = None
        self._reconnect_delay = 2.0
        self._stopped = False

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self._stopped = False
        self._worker_task = asyncio.create_task(self._worker(), name="tx-worker")
        self._connection_task = asyncio.create_task(self._maintain_connections(), name="radio-connection")

    async def stop(self) -> None:
        self._stopped = True
        self._queue_event.set()
        if self._connection_task:
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        for t in self._transports.values():
            if t.tx:
                await t.tx.close()

    async def reconfigure(self) -> None:
        """Rebuild transports from settings and connect the enabled ones.
        Called at startup and after a settings save."""
        async with self._lock:
            for t in self._transports.values():
                if t.tx:
                    try:
                        await t.tx.close()
                    except Exception:
                        pass
            self._transports = _build_transports(self._db)
            targets = [t for t in self._transports.values() if t.enabled and t.target]
            for t in targets:
                await self._ensure(t)

    async def _maintain_connections(self) -> None:
        while not self._stopped:
            await asyncio.sleep(15)
            try:
                async with self._lock:
                    for transport in self._transports.values():
                        if not transport.enabled or not transport.target:
                            continue
                        if transport.tx is not None and not transport.tx.connected:
                            transport.connected = False
                            await self._reconnect(transport)
                        elif not transport.connected or transport.tx is None:
                            await self._ensure(transport)
            except Exception:
                logger.exception("radio connection maintenance failed")

    # ---- status / compat ------------------------------------------------
    @property
    def connected(self) -> bool:
        return any(t.connected and t.tx is not None and t.tx.connected
                   for t in self._transports.values() if t.enabled)

    @property
    def port(self) -> str | None:
        return self._transports["meshcore"].target or None

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def last_error(self) -> str:
        for t in self._transports.values():
            if t.enabled and t.error:
                return "%s: %s" % (t.label, t.error)
        return ""

    def status(self) -> list[dict]:
        return [
            {"name": t.name, "label": t.label, "enabled": t.enabled,
             "conn": t.conn, "connected": t.connected and t.tx is not None and t.tx.connected,
             "target": t.target, "channel": t.channel, "error": t.error}
            for t in self._transports.values()
        ]

    def _saved_radio(self) -> MeshCoreTransmitter:
        transport = self._transports["meshcore"]
        if not transport.enabled or not transport.target or not transport.connected or not transport.tx or not transport.tx.connected:
            raise RuntimeError("saved MeshCore radio is offline")
        return transport.tx

    async def get_device_settings(self) -> dict:
        async with self._lock:
            return await self._saved_radio().read_settings()

    async def set_device_name(self, name: str) -> str:
        async with self._lock:
            return await self._saved_radio().set_device_name(name)

    async def set_tx_power(self, power: int) -> int:
        async with self._lock:
            return await self._saved_radio().set_tx_power(power)

    async def set_radio_parameters(self, freq: float, bw: float, sf: int, cr: int) -> dict:
        async with self._lock:
            return await self._saved_radio().set_radio_parameters(freq, bw, sf, cr)

    async def _refresh_saved_channels(self) -> None:
        channels = await self._saved_radio().read_channels()
        self._db.set_setting("meshcore_channels", channels)
        transport = self._transports["meshcore"]
        self._db.set_setting("meshcore_channels_target",
                             {"conn": transport.conn, "target": transport.target})

    async def rename_device_channel(self, index: int, name: str) -> dict:
        async with self._lock:
            renamed = await self._saved_radio().rename_channel(index, name)
            await self._refresh_saved_channels()
            return renamed

    async def add_device_channel(self, name: str, secret_hex: str = "") -> dict:
        async with self._lock:
            created = await self._saved_radio().add_channel(name, secret_hex)
            try:
                await self._refresh_saved_channels()
            except Exception as exc:
                # Do not lose the one-time generated key after a verified write.
                created["refresh_error"] = str(exc)
            return created

    async def remove_device_channel(self, index: int) -> None:
        async with self._lock:
            if not 1 <= index < 8:
                raise ValueError("only private channel slots 1-7 can be removed")
            if index in (self._transports["meshcore"].channel,
                         self._transports["meshcore"].test_channel):
                raise ValueError("change the Live and Test channel selections before removing this channel")
            await self._saved_radio().remove_channel(index)
            await self._refresh_saved_channels()

    async def set_port(self, port: str) -> None:
        """Set the MeshCore serial port, then reconnect."""
        self._db.set_setting("meshcore_port", port or "")
        await self.reconfigure()

    # ---- connection -----------------------------------------------------
    async def _ensure(self, t: Transport) -> bool:
        if t.connected and t.tx is not None and t.tx.connected:
            return True
        if not t.enabled:
            return False
        if not t.target:
            t.error = "no connection configured"
            return False
        err = await self._open_once(t)
        if not err:
            self._reconnect_delay = 2.0
            self._db.add_event("INFO", "%s connected (%s)" % (t.label, t.target))
            logger.info("%s connected at %s", t.name, t.target)
            return True
        t.error = err
        self._db.add_error(t.name, err)
        logger.warning("%s connect failed: %s", t.name, err)
        return False

    async def _open_once(self, t: Transport) -> str:
        """Open t's link once. Returns '' on success or an error string. No DB
        logging so callers (startup vs. reconnect) can decide how to report."""
        try:
            tx = t.make()
            await tx.connect()
            t.tx, t.connected, t.error = tx, True, ""
            return ""
        except Exception as exc:
            t.tx, t.connected = None, False
            return "connect failed: %s" % exc

    @staticmethod
    def _is_port_locked(err: str) -> bool:
        e = err.lower()
        return ("lock" in e or "busy" in e or "errno 11" in e
                or "errno 16" in e or "resource temporarily unavailable" in e)

    # ---- sending --------------------------------------------------------
    def enqueue_verification(self, text: str, on_result=None,
                             allow_new: bool = True) -> bool:
        """Keep one verification item at the end of the live warning queue."""
        pending = next((item for item in self._queue if item.verification), None)
        if pending is not None:
            self._queue.remove(pending)
            self._queue.append(pending)
            return True
        if self._verification_in_flight or not allow_new or len(self._queue) == self._queue.maxlen:
            return False
        self._queue.append(QueueItem(text=text, delay_after=BURST_GAP_SECONDS,
                                     on_result=on_result, verification=True))
        self._queue_event.set()
        return True

    def enqueue(self, text: str, channel: int | None = None, on_result=None,
                delay_after: float = BURST_GAP_SECONDS) -> bool:
        """Queue a BOM warning for the live channel. on_result(ok, err)
        fires after the send with the REAL verified outcome."""
        pending = next((item for item in self._queue if item.verification), None)
        if pending is not None:
            self._queue.remove(pending)
        dropped = len(self._queue) >= QUEUE_MAX
        if dropped:
            oldest = self._queue.popleft()
            if oldest.on_result is not None:
                self._safe_result(oldest.on_result, False, "dropped (queue full)")
        self._queue.append(QueueItem(text=text, delay_after=max(0.0, float(delay_after)), on_result=on_result))
        if pending is not None and len(self._queue) < self._queue.maxlen:
            self._queue.append(pending)
        self._queue_event.set()
        if dropped:
            logger.warning("transmit queue full; dropped oldest")
            self._db.add_event("WARN", "transmit queue full; dropped oldest message")
        return not dropped

    def _safe_result(self, cb, ok: bool, err: str = "") -> None:
        try:
            r = cb(ok, err)
            if asyncio.iscoroutine(r):
                asyncio.create_task(r)
        except Exception:
            logger.exception("on_result callback error")

    async def _reconnect(self, t: Transport) -> bool:
        """Reset a transport's link, tolerating a zombie / locked serial port.
        A just-closed port often has not released its fd yet, so an instant reopen
        fails with '[Errno 11] could not exclusively lock port', and close() itself
        can hang on a wedged reader thread. So: close firmly with a timeout, then
        reopen with backoff, waiting out a still-locked port instead of giving up."""
        t.connected = False
        if t.tx is not None:
            try:
                await asyncio.wait_for(t.tx.close(), timeout=8)  # don't hang on a stuck close
            except Exception:
                pass
        t.tx = None
        last = "not reopened"
        for i, delay in enumerate((0.5, 1.5, 3.0, 5.0)):
            await asyncio.sleep(delay)          # give the OS time to release the port
            err = await self._open_once(t)
            if not err:
                self._db.add_event(
                    "INFO", "%s link reset%s (%s)" % (
                        t.label, (" after %d tries" % (i + 1)) if i else "", t.target))
                logger.info("%s reconnected at %s", t.name, t.target)
                return True
            last = err
            if self._is_port_locked(err):
                logger.warning("%s port still locked (try %d/4), waiting: %s",
                               t.name, i + 1, err)
            else:
                logger.warning("%s reopen failed (try %d/4): %s", t.name, i + 1, err)
        t.error = last
        self._db.add_error(t.name, "link reset failed: %s" % last)
        return False

    async def _try_send(self, t: Transport, text: str, ch: int) -> tuple[bool, str]:
        """Send and confirm the radio actually transmitted. On failure, read WHY
        (TxUnsent.category) and apply the matching correction before retrying:
        wait out an airtime/queue limit, reconnect a dead link, or stop early on a
        content/config error that a retry cannot fix. Returns (ok, error)."""
        last = "not connected"
        for attempt in (1, 2, 3):
            if t.tx is None or not t.connected or not t.tx.connected:
                t.connected = False
                restored = await self._reconnect(t) if t.tx is not None else await self._ensure(t)
                if not restored:
                    last = t.error or "not connected"
                    await asyncio.sleep(1)
                    continue
            try:
                await t.tx.send_text(text, ch)
                if attempt > 1:
                    self._db.add_event(
                        "INFO", "%s sent on attempt %d (%s)" % (t.label, attempt, last))
                return True, ""
            except TxUnsent as exc:
                last = exc.detail
                t.error = last
                cat = exc.category
                logger.warning("%s not sent (attempt %d/3): %s [%s]",
                               t.name, attempt, exc.detail, cat)
                if cat in ("too_large", "no_channel"):
                    break  # a retry cannot fix bad content/config; fail fast with the reason
                if cat == "duty_cycle":
                    await asyncio.sleep(6)          # airtime cap: let the radio cool down
                elif cat == "queue_full":
                    await asyncio.sleep(3)          # let the TX queue drain
                elif cat == "link":
                    await self._reconnect(t)        # interface down / no reply: reopen it
                else:                               # "unsent": brief wait, then reconnect
                    await asyncio.sleep(2)
                    if attempt >= 2:
                        await self._reconnect(t)
            except Exception as exc:
                # Unexpected link error (Broken pipe, serial hiccup): reconnect + retry.
                last = "send failed: %s" % exc
                t.error = last
                logger.warning("%s send error (attempt %d/3): %s", t.name, attempt, exc)
                await self._reconnect(t)
        self._db.add_error(t.name, last)   # a real failure only after all corrections tried
        return False, last

    async def _send_all(self, text: str, manual: bool, on_test: bool | None = None) -> bool:
        any_ok = False
        # Validate before keying any radio: trim to the payload cap (multibyte-safe)
        # so a too-long message never gets silently rejected by the firmware.
        while len(text.encode()) > MAX_PAYLOAD_BYTES:
            text = text[:-1]
        blen = len(text.encode())
        # `on_test` picks the channel (test vs live); `manual` only tags the log
        # (auto vs manual). Automated alerts AND composed manual sends both go on
        # each radio's LIVE channel; only the Troubleshoot test uses the test channel.
        use_test = manual if on_test is None else on_test
        async with self._lock:
            # Each enabled radio gets ONE send that is VERIFIED to have gone out
            # (the send path retries internally on failure/no-transmit). No blind
            # repeats: a message is logged "sent" only when the radio confirmed it
            # actually keyed up, otherwise "failed" so the miss is visible.
            for t in self._transports.values():
                if not t.enabled:
                    continue
                ch = t.test_channel if use_test else t.channel
                ok, err = await self._try_send(t, text, ch)
                self._db.add_transmit_log(ch, blen, ok, text, manual,
                                          error=("" if ok else err), transport=t.name)
                if ok:
                    any_ok = True
                    logger.info("transmitted via %s on ch %d (verified)", t.name, ch)
        return any_ok

    async def send_manual(self, text: str) -> bool:
        # A composed manual broadcast is a real message for people, so it goes on
        # each radio's LIVE channel (logged as a manual action).
        return await self._send_all(text, manual=True, on_test=False)

    async def send_test(self, text: str) -> bool:
        # The Troubleshoot canned test goes on each radio's TEST channel.
        return await self._send_all(text, manual=True, on_test=True)

    async def load_channels(self, name: str, conn: str, port: str, host: str):
        """Read channels through the live link, or a temporary link for a new target."""
        async with self._lock:
            t = self._transports.get(name)
            if t is None or name != "meshcore":
                return None, "", "unknown radio"
            conn = conn or "serial"
            target = (host if conn == "tcp" else port) or ""
            if not target.strip():
                return None, "", "configure a USB port or network host to load channels"

            same_target = t.conn == conn and (
                t.target == target or (
                    conn == "serial" and bool(t.target) and
                    Path(t.target).resolve() == Path(target).resolve()
                )
            )
            if same_target:
                if not t.connected or t.tx is None or not t.tx.connected:
                    if not await self._ensure(t):
                        return None, "", t.error or "radio is offline"
                tx = t.tx
            else:
                tx = MeshCoreTransmitter(conn, port or "", host or "")
            try:
                if not same_target:
                    await tx.connect()
                channels = await tx.read_channels()
                try:
                    model = _fmt_model(await tx.read_info())
                except Exception:
                    model = ""
                return channels, model, ""
            except Exception as exc:
                return None, "", str(exc)
            finally:
                if not same_target:
                    try:
                        await tx.close()
                    except Exception:
                        pass

    async def send_to(self, name: str, text: str) -> tuple[bool, str]:
        """Key up a single named radio (bench testing). Returns (ok, error)."""
        blen = len(text.encode())
        async with self._lock:
            t = self._transports.get(name)
            if t is None:
                return False, "unknown radio"
            if not t.enabled:
                return False, "%s is disabled" % t.label
            ch = t.test_channel   # tests go on this radio's test channel
            ok, err = await self._try_send(t, text, ch)
            self._db.add_transmit_log(ch, blen, ok, text, True,
                                      error=("" if ok else err), transport=t.name)
            if ok:
                logger.info("test transmitted via %s on ch %d", t.name, ch)
            return ok, ("" if ok else err)

    async def resend(self, name: str, text: str, channel: int) -> tuple[bool, str]:
        """Re-transmit an exact message on a specific radio and channel, logging a
        fresh entry. Backs the transmit-log Resend button."""
        blen = len(text.encode())
        async with self._lock:
            t = self._transports.get(name)
            if t is None:
                return False, "unknown radio"
            if not t.enabled:
                return False, "%s is disabled" % t.label
            ok, err = await self._try_send(t, text, channel)
            self._db.add_transmit_log(channel, blen, ok, text, True,
                                      error=("" if ok else err), transport=t.name)
            if ok:
                logger.info("resent via %s on ch %d", t.name, channel)
            return ok, ("" if ok else err)

    async def _transmit_item(self, item: QueueItem) -> tuple[bool, str]:
        """Send one queued BOM warning on the live channel."""
        blen = len(item.text.encode())
        any_ok, last = False, ""
        async with self._lock:
            for t in self._transports.values():
                if not t.enabled:
                    continue
                ch = t.channel
                ok, err = await self._try_send(t, item.text, ch)
                self._db.add_transmit_log(ch, blen, ok, item.text, False,
                                          error=("" if ok else err), transport=t.name)
                any_ok = any_ok or ok
                if not ok:
                    last = err
        return any_ok, ("" if any_ok else last)

    async def _worker(self) -> None:
        while not self._stopped:
            if not self._queue:
                self._queue_event.clear()
                try:
                    await self._queue_event.wait()
                except asyncio.CancelledError:
                    return
                continue
            item = self._queue.popleft()
            if item.verification:
                self._verification_in_flight = True
            try:
                ok, err = await self._transmit_item(item)
                if item.on_result is not None:
                    self._safe_result(item.on_result, ok, err)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failure here (e.g. a DB write erroring on a full disk) must not
                # kill the worker -- that would silently stop ALL future broadcasts.
                logger.exception("transmit worker iteration error")
                ok = False
            finally:
                if item.verification:
                    self._verification_in_flight = False
            if self._queue and item.delay_after > 0:
                try:
                    await asyncio.sleep(item.delay_after)
                except asyncio.CancelledError:
                    return
            if not ok:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60.0)
