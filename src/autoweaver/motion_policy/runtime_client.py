"""gRPC client for the Rust motion-runtime (0.7.0 thin translation layer).

The runtime exposes two RPCs — ``WriteField`` and ``ReadField`` — that
read and write named fields on EtherCAT slaves. Field-name ↔ byte
offset translation happens on the runtime side, driven by an external
YAML contract; this Python client knows nothing about contracts (see
``docs/discuss/epson-ls6-runtime-client-open-items.md`` §F).

Design points (locked 2026-05-16):

  - Synchronous API. BT ticks are synchronous; each WriteField/ReadField
    round-trip is a few hundred microseconds, well within a tick budget.
  - One method per Value oneof variant — ``write_field_f32`` /
    ``read_field_bool`` / etc. Method name pins the type; pyright catches
    mismatches at the call site.
  - Three explicit exception classes: ``RuntimeConnectionError``,
    ``RuntimeTimeoutError``, ``RuntimeFieldError``. Callers never see
    raw ``grpc.RpcError``.
  - Context-manager lifecycle: ``with RuntimeClient(addr) as client: ...``.
  - One client instance per motion-runtime process; multiple devices
    share it by passing different ``device`` names.
"""

from __future__ import annotations

from types import TracebackType
from typing import Type

import grpc

from autoweaver.motion_policy._proto import motion_pb2, motion_pb2_grpc


__all__ = [
    "RuntimeClient",
    "RuntimeConnectionError",
    "RuntimeTimeoutError",
    "RuntimeFieldError",
]


class RuntimeConnectionError(RuntimeError):
    """gRPC channel cannot reach motion-runtime (runtime down, network unreachable)."""


class RuntimeTimeoutError(RuntimeError):
    """An RPC exceeded its deadline."""


class RuntimeFieldError(RuntimeError):
    """motion-runtime rejected the field operation.

    Carries the device name, field name, and the runtime's error string —
    typically "unknown field", "type mismatch", or "slave offline".
    """

    def __init__(self, device: str, field: str, reason: str):
        self.device = device
        self.field = field
        self.reason = reason
        super().__init__(f"{device}.{field}: {reason}")


_DEFAULT_TIMEOUT_S = 1.0


class RuntimeClient:
    """Synchronous gRPC client for motion-runtime.

    Lifecycle:
        with RuntimeClient("localhost:50051") as client:
            client.write_field_f32("ls6_1", "target_x", 100.0)
            done = client.read_field_bool("ls6_1", "done")

    Or manage the channel explicitly:
        client = RuntimeClient("localhost:50051")
        try:
            ...
        finally:
            client.close()
    """

    def __init__(self, address: str = "localhost:50051", timeout_s: float = _DEFAULT_TIMEOUT_S):
        self._address = address
        self._timeout_s = timeout_s
        self._channel: grpc.Channel = grpc.insecure_channel(address)
        self._stub = motion_pb2_grpc.MotionServiceStub(self._channel)

    # --- lifecycle ---

    def close(self) -> None:
        self._channel.close()

    def __enter__(self) -> "RuntimeClient":
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
        self._write(device, field, motion_pb2.Value(v_bool=value))

    def write_field_i32(self, device: str, field: str, value: int) -> None:
        self._write(device, field, motion_pb2.Value(v_i32=value))

    def write_field_u32(self, device: str, field: str, value: int) -> None:
        self._write(device, field, motion_pb2.Value(v_u32=value))

    def write_field_i64(self, device: str, field: str, value: int) -> None:
        self._write(device, field, motion_pb2.Value(v_i64=value))

    def write_field_u64(self, device: str, field: str, value: int) -> None:
        self._write(device, field, motion_pb2.Value(v_u64=value))

    def write_field_f32(self, device: str, field: str, value: float) -> None:
        self._write(device, field, motion_pb2.Value(v_f32=value))

    def write_field_f64(self, device: str, field: str, value: float) -> None:
        self._write(device, field, motion_pb2.Value(v_f64=value))

    def write_field_bytes(self, device: str, field: str, value: bytes) -> None:
        self._write(device, field, motion_pb2.Value(v_bytes=value))

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

    # --- internals ---

    def _write(self, device: str, field: str, value: motion_pb2.Value) -> None:
        req = motion_pb2.WriteFieldRequest(device=device, field=field, value=value)
        try:
            resp = self._stub.WriteField(req, timeout=self._timeout_s)
        except grpc.RpcError as e:
            raise self._translate_rpc_error(e) from e
        if not resp.ok:
            raise RuntimeFieldError(device, field, resp.error)

    def _read(self, device: str, field: str, expected_variant: str):
        req = motion_pb2.ReadFieldRequest(device=device, field=field)
        try:
            resp = self._stub.ReadField(req, timeout=self._timeout_s)
        except grpc.RpcError as e:
            raise self._translate_rpc_error(e) from e
        if not resp.ok:
            raise RuntimeFieldError(device, field, resp.error)
        actual_variant = resp.value.WhichOneof("kind")
        if actual_variant != expected_variant:
            raise RuntimeFieldError(
                device,
                field,
                f"type mismatch: caller expected {expected_variant}, "
                f"runtime returned {actual_variant}",
            )
        return getattr(resp.value, expected_variant)

    @staticmethod
    def _translate_rpc_error(e: grpc.RpcError) -> RuntimeError:
        # grpc.RpcError instances are also grpc.Call instances exposing
        # .code() / .details() — the typing in grpcio is loose so we
        # access them defensively.
        code = getattr(e, "code", lambda: None)()
        details = getattr(e, "details", lambda: "")() or ""
        if code == grpc.StatusCode.DEADLINE_EXCEEDED:
            return RuntimeTimeoutError(f"motion-runtime RPC timed out: {details}")
        if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.UNKNOWN):
            return RuntimeConnectionError(
                f"motion-runtime unreachable (code={code}): {details}"
            )
        return RuntimeConnectionError(
            f"motion-runtime RPC failed (code={code}): {details}"
        )
