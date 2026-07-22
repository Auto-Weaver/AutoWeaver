"""Concrete pymodbus-backed ``RegisterIO`` — the transport under the engine.

``modbus_primitive`` (EVO-009) runs ``write`` / ``read`` / ``read_until`` over an
abstract :class:`~autoweaver.comm.modbus_primitive.RegisterIO`. This module is
the one layer down it promised: a real Modbus-TCP socket implementing that
interface. Everything above it stays hardware-free; everything TCP/pymodbus
lives here.

Scope is deliberately the **transport substrate only** — own the socket, the
REAL(32) word/byte ordering, register read/write, an optional heartbeat. There
is no register map and no business handshake here: every method takes an
explicit protocol register address (e.g. 41068), and the request/ack dance
(who sets which flag, when) is a declared contract the ``CommEngine`` runs, not
code in this file.

PLC register numbers are written as 4xxxx addresses (e.g. 41068). pymodbus
expects zero-based holding-register offsets, so this module converts with
``offset = register - base`` (base defaults to 40001).

The word-order default and offset convention were verified on the real rig
(pluck's ``PlcModbus``). In particular the default REAL(32) order is CDAB (a
word swap), which is what the PLC was observed to publish; do not change it
without re-checking on hardware.
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import Optional, Sequence

from pymodbus.client import ModbusTcpClient

from .modbus_primitive import RegisterIO

logger = logging.getLogger(__name__)


class ModbusProtocol(RegisterIO):
    """Modbus-TCP :class:`RegisterIO`: connect, heartbeat, register + REAL32 I/O.

    All addressing is by protocol register number (e.g. 41068). The only fixed
    conventions are ``base`` (default 40001) for offset conversion and the
    REAL(32) word order (default CDAB, verified on hardware).

    One TCP socket, one client: the pymodbus sync client is **not** thread-safe,
    so every transaction (request + response framing) is serialized under an I/O
    lock. This lets an optional heartbeat thread run without splicing its frame
    into a concurrent register read.
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        *,
        unit_id: int = 1,
        timeout_s: float = 1.0,
        base: int = 40001,
        float_word_order: str = "CDAB",
        heartbeat_interval_s: float = 1.0,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.unit_id = int(unit_id)
        self.timeout_s = float(timeout_s)
        self.base = int(base)
        self.float_word_order = str(float_word_order)
        self.heartbeat_interval_s = float(heartbeat_interval_s)

        self._client = ModbusTcpClient(host=self.host, port=self.port, timeout=self.timeout_s)
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_value = 0
        # One TCP socket, pymodbus sync client is NOT thread-safe — serialize every
        # transaction so the heartbeat thread (and any concurrent poll) can't
        # interleave request/response framing with the main loop's I/O.
        self._io_lock = threading.Lock()

    # -------------------- connection --------------------

    def connect(self) -> None:
        if not self._client.connect():
            raise RuntimeError(f"Failed to connect PLC ModbusTCP {self.host}:{self.port}")
        logger.info("Connected PLC ModbusTCP %s:%s", self.host, self.port)

    def close(self) -> None:
        self.stop_heartbeat()
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    # -------------------- heartbeat (optional; not auto-started) --------------------

    def start_heartbeat(self, register: int, interval_s: Optional[float] = None) -> None:
        """Start a background thread toggling ``register`` 0/1 at ``interval_s``.

        ``interval_s`` defaults to ``heartbeat_interval_s`` from construction.
        Idempotent: a second call while a heartbeat runs is a no-op.
        """
        if self._heartbeat_thread is not None:
            return
        interval = self.heartbeat_interval_s if interval_s is None else float(interval_s)
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(int(register), interval),
            daemon=True,
            name="plc-heartbeat",
        )
        self._heartbeat_thread.start()
        logger.info("PLC heartbeat started: register=%d interval=%.3fs", register, interval)

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None

    def _heartbeat_loop(self, register: int, interval_s: float) -> None:
        while not self._heartbeat_stop.is_set():
            try:
                self._heartbeat_value = 0 if self._heartbeat_value else 1
                self.write_u16(register, self._heartbeat_value)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Heartbeat write failed: %s", exc)
            self._heartbeat_stop.wait(max(0.1, interval_s))

    # -------------------- RegisterIO: REAL32 blocks --------------------

    def read_real32_block(self, start: int, count: int) -> list[float]:
        """Read ``count`` REAL(32) values (each 2 words) starting at ``start``."""
        words = self.read_words(start, count * 2)
        return [
            self.words_to_float(words[i], words[i + 1]) for i in range(0, len(words), 2)
        ]

    def write_real32_block(self, start: int, values: Sequence[float]) -> None:
        """Write a contiguous block of REAL(32) values starting at ``start``."""
        vals = [float(v) for v in values]
        logger.info(
            "PC write REAL32 block: start=%d values=%s",
            int(start),
            [round(v, 6) for v in vals],
        )
        words: list[int] = []
        for value in vals:
            words.extend(self.float_to_words(value))
        self.write_words(start, words)

    # -------------------- RegisterIO: raw register I/O --------------------

    def to_offset(self, register: int) -> int:
        return int(register) - self.base

    def read_u16(self, register: int) -> int:
        result = self._modbus_call(
            "read_holding_registers", address=self.to_offset(register), count=1
        )
        if self._is_modbus_error(result):
            raise RuntimeError(f"read_u16 failed register={register}: {result}")
        return int(result.registers[0])

    def write_u16(self, register: int, value: int) -> None:
        result = self._modbus_call(
            "write_register", address=self.to_offset(register), value=int(value) & 0xFFFF
        )
        if self._is_modbus_error(result):
            raise RuntimeError(f"write_u16 failed register={register}, value={value}: {result}")

    def read_words(self, start_register: int, count: int) -> list[int]:
        result = self._modbus_call(
            "read_holding_registers", address=self.to_offset(start_register), count=int(count)
        )
        if self._is_modbus_error(result):
            raise RuntimeError(f"read_words failed start={start_register}, count={count}: {result}")
        return [int(x) for x in result.registers]

    def write_words(self, start_register: int, words: Sequence[int]) -> None:
        result = self._modbus_call(
            "write_registers",
            address=self.to_offset(start_register),
            values=[int(x) & 0xFFFF for x in words],
        )
        if self._is_modbus_error(result):
            raise RuntimeError(f"write_words failed start={start_register}, words={words}: {result}")

    # -------------------- pymodbus version compatibility --------------------

    def _modbus_call(self, method_name: str, **kwargs):
        """Call pymodbus version-compatibly.

        The unit-id keyword drifted across pymodbus releases: 3.9+ uses
        ``device_id=``, older 3.x used ``slave=``, and some builds accepted
        neither on certain calls (``unit=`` in the 2.x lineage). Try the
        variants in that order so the same code runs on the rig and in dev.
        """
        method = getattr(self._client, method_name)
        attempts = [
            {**kwargs, "device_id": self.unit_id},
            {**kwargs, "slave": self.unit_id},
            {**kwargs, "unit": self.unit_id},
            dict(kwargs),
        ]
        last_type_error: Optional[TypeError] = None
        # Hold the lock around the whole transaction (request + response read) so a
        # concurrent thread can't splice its own frame into the middle.
        with self._io_lock:
            for call_kwargs in attempts:
                try:
                    return method(**call_kwargs)
                except TypeError as exc:
                    message = str(exc)
                    if "unexpected keyword argument" in message or "got an unexpected" in message:
                        last_type_error = exc
                        continue
                    raise
        if last_type_error is not None:
            raise last_type_error
        raise RuntimeError(f"Modbus call failed unexpectedly: {method_name}")

    @staticmethod
    def _is_modbus_error(result) -> bool:
        is_error = getattr(result, "isError", None)
        if callable(is_error):
            return bool(is_error())
        return bool(is_error)

    # -------------------- REAL(32) word/byte order --------------------

    def _float_order(self) -> str:
        """Normalize the configured REAL(32) word/byte order to ABCD/CDAB/BADC/DCBA.

        One REAL occupies two 16-bit holding registers. PLC tools describe the
        order as ABCD / CDAB / BADC / DCBA; legacy aliases (high_first/low_first)
        are accepted for compatibility.
        """
        raw = str(self.float_word_order or "ABCD").strip().upper()
        aliases = {
            "HIGH_FIRST": "ABCD", "HIGH": "ABCD", "ABCD": "ABCD", "BIG": "ABCD", "BIG_ENDIAN": "ABCD",
            "LOW_FIRST": "CDAB", "LOW": "CDAB", "WORD_SWAP": "CDAB", "SWAP_WORD": "CDAB", "CDAB": "CDAB",
            "BYTE_SWAP": "BADC", "BADC": "BADC",
            "WORD_BYTE_SWAP": "DCBA", "BYTE_WORD_SWAP": "DCBA", "DCBA": "DCBA",
        }
        if raw not in aliases:
            raise ValueError(
                "Unsupported float_word_order={!r}. Use ABCD, CDAB, BADC, DCBA, "
                "or aliases high_first/low_first.".format(self.float_word_order)
            )
        return aliases[raw]

    @staticmethod
    def _word_to_bytes(word: int) -> tuple[int, int]:
        word = int(word) & 0xFFFF
        return (word >> 8) & 0xFF, word & 0xFF

    @staticmethod
    def _bytes_to_word(high_byte: int, low_byte: int) -> int:
        return ((int(high_byte) & 0xFF) << 8) | (int(low_byte) & 0xFF)

    def float_to_words(self, value: float) -> tuple[int, int]:
        # Canonical IEEE754 big-endian byte sequence: A B C D.
        a, b, c, d = struct.pack(">f", float(value))
        order = self._float_order()
        if order == "ABCD":
            return self._bytes_to_word(a, b), self._bytes_to_word(c, d)
        if order == "CDAB":
            return self._bytes_to_word(c, d), self._bytes_to_word(a, b)
        if order == "BADC":
            return self._bytes_to_word(b, a), self._bytes_to_word(d, c)
        if order == "DCBA":
            return self._bytes_to_word(d, c), self._bytes_to_word(b, a)
        raise AssertionError(order)

    def words_to_float(self, word0: int, word1: int) -> float:
        # Register-order bytes as read from Modbus.
        w0h, w0l = self._word_to_bytes(word0)
        w1h, w1l = self._word_to_bytes(word1)
        order = self._float_order()
        if order == "ABCD":
            raw = bytes([w0h, w0l, w1h, w1l])
        elif order == "CDAB":
            raw = bytes([w1h, w1l, w0h, w0l])
        elif order == "BADC":
            raw = bytes([w0l, w0h, w1l, w1h])
        elif order == "DCBA":
            raw = bytes([w1l, w1h, w0l, w0h])
        else:
            raise AssertionError(order)
        return float(struct.unpack(">f", raw)[0])


__all__ = ["ModbusProtocol"]
