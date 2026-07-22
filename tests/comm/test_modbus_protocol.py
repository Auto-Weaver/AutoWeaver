"""Tests for the concrete pymodbus-backed transport (ModbusProtocol).

Hardware-free: a ``FakeModbusClient`` stands in for pymodbus' ``ModbusTcpClient``
(patched in at construction), so nothing touches a socket. We exercise the
transport's own concerns — REAL(32) word-order codec, 4xxxx→offset conversion,
register counts per REAL value, the unit-id keyword compat shim, and that the
I/O lock is in place.
"""

from __future__ import annotations

import struct
import threading

import pytest

from autoweaver.comm import ModbusProtocol
from autoweaver.comm.modbus_primitive import RegisterIO


# --------------------------------------------------------------------------- #
# Fake pymodbus client
# --------------------------------------------------------------------------- #
class FakeResult:
    """Minimal stand-in for a pymodbus response object."""

    def __init__(self, registers=None, error=False):
        self.registers = list(registers or [])
        self._error = error

    def isError(self) -> bool:
        return self._error


class FakeModbusClient:
    """In-memory holding registers. Records calls and the unit-id kwarg seen.

    ``accept_ids`` restricts which unit-id keyword names it will tolerate, so a
    test can simulate an older pymodbus that only knows ``slave=`` and prove the
    compat shim falls through to it.
    """

    def __init__(self, *, host=None, port=None, timeout=None, accept_ids=("device_id",)):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.mem: dict[int, int] = {}
        self.calls: list[tuple] = []
        self.unit_ids_seen: list[int] = []
        self._accept_ids = set(accept_ids)
        self.connected = False

    def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    def _take_unit(self, kwargs):
        for name in ("device_id", "slave", "unit"):
            if name in kwargs:
                if name not in self._accept_ids:
                    raise TypeError(f"{name}() got an unexpected keyword argument {name!r}")
                self.unit_ids_seen.append(kwargs[name])
                return {k: v for k, v in kwargs.items() if k != name}
        return dict(kwargs)

    def read_holding_registers(self, address, **kwargs):
        count = kwargs.pop("count", 1)
        self._take_unit(kwargs)
        self.calls.append(("read", address, count))
        regs = [int(self.mem.get(address + i, 0)) for i in range(int(count))]
        return FakeResult(registers=regs)

    def write_register(self, address, value, **kwargs):
        self._take_unit(kwargs)
        self.calls.append(("write_one", address, int(value)))
        self.mem[address] = int(value) & 0xFFFF
        return FakeResult()

    def write_registers(self, address, values, **kwargs):
        self._take_unit(kwargs)
        vals = [int(v) & 0xFFFF for v in values]
        self.calls.append(("write_many", address, vals))
        for i, v in enumerate(vals):
            self.mem[address + i] = v
        return FakeResult()


@pytest.fixture
def make_proto(monkeypatch):
    """Build a ModbusProtocol whose pymodbus client is a FakeModbusClient.

    Returns (protocol, fake_client). Extra kwargs pass through to the protocol;
    ``accept_ids`` (if given) is routed to the fake client instead.
    """

    def _make(*, accept_ids=("device_id",), **proto_kwargs):
        created = {}

        def _factory(*, host, port, timeout):
            client = FakeModbusClient(host=host, port=port, timeout=timeout, accept_ids=accept_ids)
            created["client"] = client
            return client

        monkeypatch.setattr("autoweaver.comm.modbus_protocol.ModbusTcpClient", _factory)
        proto = ModbusProtocol("192.168.0.10", **proto_kwargs)
        return proto, created["client"]

    return _make


# --------------------------------------------------------------------------- #
# It really is a RegisterIO
# --------------------------------------------------------------------------- #
def test_is_registerio_subclass(make_proto):
    proto, _ = make_proto()
    assert isinstance(proto, RegisterIO)


# --------------------------------------------------------------------------- #
# REAL(32) word-order codec — round-trip in both CDAB and ABCD
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("order", ["CDAB", "ABCD", "BADC", "DCBA"])
@pytest.mark.parametrize("value", [0.0, 1.0, -1.0, 3.14159, 1234.5, -0.001953125])
def test_real32_word_roundtrip(make_proto, order, value):
    proto, _ = make_proto(float_word_order=order)
    w0, w1 = proto.float_to_words(value)
    assert 0 <= w0 <= 0xFFFF and 0 <= w1 <= 0xFFFF
    back = proto.words_to_float(w0, w1)
    assert back == pytest.approx(value, rel=1e-6, abs=1e-6)


def test_abcd_is_ieee754_big_endian(make_proto):
    # ABCD lays the canonical IEEE754 big-endian bytes straight into (w0, w1).
    proto, _ = make_proto(float_word_order="ABCD")
    a, b, c, d = struct.pack(">f", 3.14159)
    w0, w1 = proto.float_to_words(3.14159)
    assert w0 == (a << 8) | b
    assert w1 == (c << 8) | d


def test_cdab_is_abcd_with_words_swapped(make_proto):
    # CDAB == ABCD with the two 16-bit words swapped. Verified-on-hardware default.
    p_abcd, _ = make_proto(float_word_order="ABCD")
    p_cdab, _ = make_proto(float_word_order="CDAB")
    a0, a1 = p_abcd.float_to_words(1234.5)
    c0, c1 = p_cdab.float_to_words(1234.5)
    assert (c0, c1) == (a1, a0)


def test_default_word_order_is_cdab(make_proto):
    proto, _ = make_proto()
    assert proto.float_word_order == "CDAB"


def test_unknown_word_order_raises(make_proto):
    proto, _ = make_proto(float_word_order="ZZZZ")
    with pytest.raises(ValueError):
        proto.float_to_words(1.0)


# --------------------------------------------------------------------------- #
# 4xxxx protocol address -> zero-based offset conversion
# --------------------------------------------------------------------------- #
def test_offset_conversion_default_base(make_proto):
    proto, _ = make_proto()  # base defaults to 40001
    assert proto.to_offset(41068) == 1067
    assert proto.to_offset(40001) == 0


def test_offset_conversion_custom_base(make_proto):
    proto, _ = make_proto(base=40000)
    assert proto.to_offset(41068) == 1068


def test_read_u16_uses_offset(make_proto):
    proto, client = make_proto()
    client.mem[1067] = 7  # offset for 41068
    assert proto.read_u16(41068) == 7
    assert client.calls[-1] == ("read", 1067, 1)


def test_write_u16_uses_offset_and_masks(make_proto):
    proto, client = make_proto()
    proto.write_u16(41068, 0x1_0005)  # value wider than 16 bits
    assert client.calls[-1] == ("write_one", 1067, 5)
    assert client.mem[1067] == 5


# --------------------------------------------------------------------------- #
# REAL32 blocks -> two holding registers per value
# --------------------------------------------------------------------------- #
def test_write_real32_block_uses_two_registers_per_value(make_proto):
    proto, client = make_proto()
    proto.write_real32_block(41183, [1.0, 2.0, 3.0])
    kind, address, words = client.calls[-1]
    assert kind == "write_many"
    assert address == proto.to_offset(41183)
    assert len(words) == 6  # 3 REAL32 values * 2 registers each


def test_read_real32_block_reads_two_registers_per_value(make_proto):
    proto, client = make_proto()
    proto.read_real32_block(41115, 4)
    kind, address, count = client.calls[-1]
    assert kind == "read"
    assert address == proto.to_offset(41115)
    assert count == 8  # 4 values * 2 registers each


def test_real32_block_roundtrip_through_registers(make_proto):
    # Write a block, then read it back from the same in-memory registers.
    proto, _ = make_proto(float_word_order="CDAB")
    values = [12.5, -7.25, 0.0, 1000.125, -3.5, 42.0]
    proto.write_real32_block(41183, values)
    back = proto.read_real32_block(41183, len(values))
    assert back == pytest.approx(values)


def test_real32_block_roundtrip_abcd(make_proto):
    proto, _ = make_proto(float_word_order="ABCD")
    values = [1.0, -2.0, 3.5]
    proto.write_real32_block(41000, values)
    assert proto.read_real32_block(41000, 3) == pytest.approx(values)


# --------------------------------------------------------------------------- #
# unit-id keyword compat shim
# --------------------------------------------------------------------------- #
def test_passes_device_id_on_modern_pymodbus(make_proto):
    proto, client = make_proto(unit_id=3)  # fake accepts only device_id
    proto.read_u16(41068)
    assert client.unit_ids_seen[-1] == 3


def test_falls_back_to_slave_on_older_pymodbus(make_proto):
    # An older client rejects device_id but accepts slave — the shim must retry.
    proto, client = make_proto(unit_id=5, accept_ids=("slave",))
    proto.read_u16(41068)
    assert client.unit_ids_seen[-1] == 5


# --------------------------------------------------------------------------- #
# connection lifecycle + I/O lock smoke
# --------------------------------------------------------------------------- #
def test_connect_and_close(make_proto):
    proto, client = make_proto()
    proto.connect()
    assert client.connected is True
    proto.close()
    assert client.connected is False


def test_connect_failure_raises(make_proto, monkeypatch):
    proto, client = make_proto()
    monkeypatch.setattr(client, "connect", lambda: False)
    with pytest.raises(RuntimeError):
        proto.connect()


def test_io_lock_present_and_usable(make_proto):
    proto, _ = make_proto()
    # A real lock guarding every transaction; acquirable and releasable.
    assert isinstance(proto._io_lock, type(threading.Lock()))
    assert proto._io_lock.acquire(blocking=False)
    proto._io_lock.release()


def test_heartbeat_not_started_by_default(make_proto):
    proto, _ = make_proto()
    assert proto._heartbeat_thread is None
