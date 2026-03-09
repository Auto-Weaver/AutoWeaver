"""Modbus-based comm signal adapter (production-side transport).

Principle: keep the I/O layer free of business semantics; this class only does
register read/write + handshake. Register mapping (request_target / pick_done /
reset, payload layout) should be defined by upper layers later.

Provided now:
- Request/ack handshake on a single holding register (default D0.bit0=request,
  bit1=ack)
- Optional payload encode/decode callbacks for future mapping
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Dict, Any

from pymodbus.client import ModbusTcpClient

from .base import CommSignalBase

logger = logging.getLogger(__name__)


def _get_bit(value: int, bit: int) -> bool:
    return bool((value >> bit) & 0x1)


def _set_bit(value: int, bit: int, state: bool) -> int:
    mask = 1 << bit
    return (value | mask) if state else (value & ~mask)


class ModbusAdapter(CommSignalBase):
    """Modbus TCP comm-signal transport.

    Parameters
    ----------
    host, port, unit_id, timeout :
        Standard Modbus TCP connection settings.
    flag_register :
        Holding register used for handshake (zero-based, 0 -> 40001).
    request_bit / ack_bit :
        Bit positions inside flag register; default bit0=PLC request, bit1=PC ack.
    decode_payload :
        Optional callback `decode_payload(reg_value, client) -> dict | None` to turn
        registers into a message dict. If missing, only the handshake envelope is
        returned.
    encode_payload :
        Optional callback `encode_payload(message, client)` to write response payload
        (coords/status/etc.). Handshake bits are handled by the adapter.
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 1,
        timeout: float = 1.0,
        *,
        flag_register: int = 0,
        request_bit: int = 0,
        ack_bit: int = 1,
        decode_payload: Optional[Callable[[int, ModbusTcpClient], Optional[Dict[str, Any]]]] = None,
        encode_payload: Optional[Callable[[Dict[str, Any], ModbusTcpClient], None]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self.flag_register = flag_register
        self.request_bit = request_bit
        self.ack_bit = ack_bit
        self.decode_payload = decode_payload
        self.encode_payload = encode_payload

        self._client = ModbusTcpClient(host=self.host, port=self.port, timeout=self.timeout)
        if not self._client.connect():
            raise ConnectionError(f"Failed to connect Modbus TCP {self.host}:{self.port}")

    # ------------------------------------------------------------------ #
    # CommSignalBase
    # ------------------------------------------------------------------ #
    def receive(self) -> Optional[Dict[str, Any]]:
        """Non-blocking read of comm request.

        Returns a dict with handshake envelope:
            {"request_id": int, "raw_flag": int, **payload}
        Returns None when no new request is present.
        """
        flag = self._read_flag()
        if flag is None:
            return None

        has_req = _get_bit(flag, self.request_bit)
        has_ack = _get_bit(flag, self.ack_bit)

        # If PLC already cleared request but ack is still 1, clear ack to finish handshake.
        if not has_req and has_ack:
            self._write_flag(_set_bit(flag, self.ack_bit, False))
            return None

        # No pending request
        if not has_req or has_ack:
            return None

        payload: Dict[str, Any] = {}
        if self.decode_payload:
            try:
                decoded = self.decode_payload(flag, self._client)
                if decoded:
                    payload.update(decoded)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Modbus decode_payload failed: %s", exc)

        request_id = int(time.time() * 1000)
        return {"request_id": request_id, "raw_flag": flag, **payload}

    def send(self, message: Dict[str, Any]) -> None:
        """Write response + ack bit."""
        flag = self._read_flag()
        if flag is None:
            return

        # Write payload first (if a writer is provided)
        if self.encode_payload:
            try:
                self.encode_payload(message, self._client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Modbus encode_payload failed: %s", exc)

        # Set ack bit = 1
        updated = _set_bit(flag, self.ack_bit, True)
        self._write_flag(updated)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to close Modbus client: %s", exc)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _read_flag(self) -> Optional[int]:
        try:
            resp = self._client.read_holding_registers(
                address=self.flag_register,
                count=1,
                unit=self.unit_id,
            )
            if resp.isError():
                logger.warning("Modbus read error: %s", resp)
                return None
            return resp.registers[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Modbus read exception: %s", exc)
            return None

    def _write_flag(self, value: int) -> None:
        try:
            resp = self._client.write_register(
                address=self.flag_register,
                value=value,
                unit=self.unit_id,
            )
            if resp.isError():
                logger.warning("Modbus write error: %s", resp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Modbus write exception: %s", exc)
