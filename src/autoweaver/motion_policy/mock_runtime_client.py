"""In-memory mock of ``RuntimeClient`` for driver tests.

Mirrors the public surface of ``RuntimeClient`` exactly so a driver (e.g.
``EpsonLS6``) under test can swap one for the other. Stores fields in a
per-device dict and tracks every write/read in ``self.calls`` for
assertions.

This is a test double, not a runtime artifact — drivers depend on the
``RuntimeClient`` shape, not on this specific class.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Type

from autoweaver.motion_policy.runtime_client import RuntimeFieldError


__all__ = ["MockRuntimeClient", "MockWriteBatch"]


class MockRuntimeClient:
    """In-memory stand-in for ``RuntimeClient``.

    Behavior:
      - Each ``write_field_*`` stores the value into a per-device dict
        keyed by (device, field). The Python type recorded matches the
        method called — calling ``write_field_f32`` stores a ``float``,
        ``write_field_i32`` stores an ``int``, etc.
      - Each ``read_field_*`` returns the last-written value. Reading an
        unset field raises ``RuntimeFieldError`` (mirrors the runtime's
        "unknown field" response).
      - Type-mismatched reads (e.g. ``read_field_bool`` after writing via
        ``write_field_i32``) also raise ``RuntimeFieldError``.
      - All calls are appended to ``self.calls`` as
        ``(op, device, field, value)`` tuples for assertions; reads use
        the value field for the value returned.

    Batch writes (``client.batch(device).f32(...).commit()``) apply all
    fields atomically and record as a single
    ``("batch_write", device, [(field, variant, value), ...])`` entry in
    ``self.calls`` — mirrors the all-or-nothing semantics of the real
    ``WriteFields`` RPC.

    Test helpers:
      - ``preload(device, field, value, variant)`` seeds a value without
        touching ``self.calls``.
    """

    # Python type → expected variant string (kept in sync with proto).
    _WRITE_VARIANTS = {
        "write_field_bool":  "v_bool",
        "write_field_i32":   "v_i32",
        "write_field_u32":   "v_u32",
        "write_field_i64":   "v_i64",
        "write_field_u64":   "v_u64",
        "write_field_f32":   "v_f32",
        "write_field_f64":   "v_f64",
        "write_field_bytes": "v_bytes",
    }

    def __init__(self) -> None:
        # Per-device field store: (device, field) -> (variant, value)
        self._store: dict[tuple[str, str], tuple[str, Any]] = {}
        self.calls: list[tuple] = []

    # --- lifecycle (parity with RuntimeClient) ---

    def close(self) -> None:
        pass

    def __enter__(self) -> "MockRuntimeClient":
        return self

    def __exit__(
        self,
        exc_type: Type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- write ---

    def write_field_bool(self, device: str, field: str, value: bool) -> None:
        self._write(device, field, "v_bool", value)

    def write_field_i32(self, device: str, field: str, value: int) -> None:
        self._write(device, field, "v_i32", value)

    def write_field_u32(self, device: str, field: str, value: int) -> None:
        self._write(device, field, "v_u32", value)

    def write_field_i64(self, device: str, field: str, value: int) -> None:
        self._write(device, field, "v_i64", value)

    def write_field_u64(self, device: str, field: str, value: int) -> None:
        self._write(device, field, "v_u64", value)

    def write_field_f32(self, device: str, field: str, value: float) -> None:
        self._write(device, field, "v_f32", value)

    def write_field_f64(self, device: str, field: str, value: float) -> None:
        self._write(device, field, "v_f64", value)

    def write_field_bytes(self, device: str, field: str, value: bytes) -> None:
        self._write(device, field, "v_bytes", value)

    # --- batch write ---

    def batch(self, device: str) -> "MockWriteBatch":
        return MockWriteBatch(self, device)

    # --- read ---

    def read_field_bool(self, device: str, field: str) -> bool:
        return self._read(device, field, "v_bool")

    def read_field_i32(self, device: str, field: str) -> int:
        return self._read(device, field, "v_i32")

    def read_field_u32(self, device: str, field: str) -> int:
        return self._read(device, field, "v_u32")

    def read_field_i64(self, device: str, field: str) -> int:
        return self._read(device, field, "v_i64")

    def read_field_u64(self, device: str, field: str) -> int:
        return self._read(device, field, "v_u64")

    def read_field_f32(self, device: str, field: str) -> float:
        return self._read(device, field, "v_f32")

    def read_field_f64(self, device: str, field: str) -> float:
        return self._read(device, field, "v_f64")

    def read_field_bytes(self, device: str, field: str) -> bytes:
        return self._read(device, field, "v_bytes")

    # --- test helpers ---

    def preload(self, device: str, field: str, value: Any, variant: str) -> None:
        """Seed a field without recording the call. For test setup only."""
        self._store[(device, field)] = (variant, value)

    # --- internals ---

    def _write(self, device: str, field: str, variant: str, value: Any) -> None:
        self._store[(device, field)] = (variant, value)
        self.calls.append(("write", device, field, value))

    def _read(self, device: str, field: str, expected_variant: str) -> Any:
        stored = self._store.get((device, field))
        if stored is None:
            raise RuntimeFieldError(device, field, "unknown field")
        stored_variant, value = stored
        if stored_variant != expected_variant:
            raise RuntimeFieldError(
                device,
                field,
                f"type mismatch: caller expected {expected_variant}, "
                f"stored as {stored_variant}",
            )
        self.calls.append(("read", device, field, value))
        return value

    # --- internals used by MockWriteBatch ---

    def _commit_batch(
        self, device: str, fields: list[tuple[str, str, Any]]
    ) -> None:
        for field, variant, value in fields:
            self._store[(device, field)] = (variant, value)
        self.calls.append(("batch_write", device, list(fields)))


class MockWriteBatch:
    """In-memory equivalent of ``WriteBatch`` for driver tests.

    Mirrors the chainable builder shape but commits straight into the
    mock client's store. All fields apply atomically (i.e. the test
    can't observe a partial commit), matching the real runtime contract.
    """

    def __init__(self, client: MockRuntimeClient, device: str):
        self._client = client
        self._device = device
        self._fields: list[tuple[str, str, Any]] = []

    def bool(self, field: str, value: bool) -> "MockWriteBatch":
        self._fields.append((field, "v_bool", value))
        return self

    def i32(self, field: str, value: int) -> "MockWriteBatch":
        self._fields.append((field, "v_i32", value))
        return self

    def u32(self, field: str, value: int) -> "MockWriteBatch":
        self._fields.append((field, "v_u32", value))
        return self

    def i64(self, field: str, value: int) -> "MockWriteBatch":
        self._fields.append((field, "v_i64", value))
        return self

    def u64(self, field: str, value: int) -> "MockWriteBatch":
        self._fields.append((field, "v_u64", value))
        return self

    def f32(self, field: str, value: float) -> "MockWriteBatch":
        self._fields.append((field, "v_f32", value))
        return self

    def f64(self, field: str, value: float) -> "MockWriteBatch":
        self._fields.append((field, "v_f64", value))
        return self

    def bytes(self, field: str, value: bytes) -> "MockWriteBatch":
        self._fields.append((field, "v_bytes", value))
        return self

    def commit(self) -> None:
        if not self._fields:
            return
        self._client._commit_batch(self._device, self._fields)
