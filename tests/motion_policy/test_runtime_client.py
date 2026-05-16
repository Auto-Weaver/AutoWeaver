from __future__ import annotations

from typing import Callable

import grpc
import pytest

from autoweaver.motion_policy._proto import motion_pb2
from autoweaver.motion_policy.runtime_client import (
    RuntimeClient,
    RuntimeConnectionError,
    RuntimeFieldError,
    RuntimeTimeoutError,
)


# ---------------------------------------------------------------------------
# Test stubs
# ---------------------------------------------------------------------------


class _FakeRpcError(grpc.RpcError):
    """Mimics what grpcio raises — also implements .code() / .details()."""

    def __init__(self, code: grpc.StatusCode, details: str = ""):
        self._code = code
        self._details = details
        super().__init__(details)

    def code(self):
        return self._code

    def details(self):
        return self._details


class _FakeStub:
    """Captures calls and returns canned responses.

    ``write_response`` and ``read_response`` can be either a response
    message or a zero-argument callable that produces one (or raises)
    — the latter lets a test simulate transport errors.
    """

    def __init__(self):
        self.write_calls: list[motion_pb2.WriteFieldRequest] = []
        self.read_calls: list[motion_pb2.ReadFieldRequest] = []
        self.write_response: (
            motion_pb2.WriteFieldResponse
            | Callable[[], motion_pb2.WriteFieldResponse]
        ) = motion_pb2.WriteFieldResponse(ok=True)
        self.read_response: (
            motion_pb2.ReadFieldResponse
            | Callable[[], motion_pb2.ReadFieldResponse]
            | None
        ) = None

    def WriteField(self, req, timeout=None):
        self.write_calls.append(req)
        if callable(self.write_response):
            return self.write_response()
        return self.write_response

    def ReadField(self, req, timeout=None):
        self.read_calls.append(req)
        if self.read_response is None:
            raise AssertionError("test forgot to set read_response")
        if callable(self.read_response):
            return self.read_response()
        return self.read_response


def _client_with_stub(stub: _FakeStub) -> RuntimeClient:
    """Construct a RuntimeClient and swap its stub for the fake."""
    client = RuntimeClient(address="ignored")
    client._stub = stub  # type: ignore[assignment]
    return client


# ---------------------------------------------------------------------------
# write_field_* encodes the right Value variant
# ---------------------------------------------------------------------------


def test_write_field_f32_sets_v_f32_variant():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    client.write_field_f32("ls6_1", "target_x", 100.5)
    assert len(stub.write_calls) == 1
    req = stub.write_calls[0]
    assert req.device == "ls6_1"
    assert req.field == "target_x"
    assert req.value.WhichOneof("kind") == "v_f32"
    assert req.value.v_f32 == pytest.approx(100.5)


def test_write_field_bool_sets_v_bool_variant():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    client.write_field_bool("ls6_1", "trigger", True)
    req = stub.write_calls[0]
    assert req.value.WhichOneof("kind") == "v_bool"
    assert req.value.v_bool is True


def test_write_field_i32_sets_v_i32_variant():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    client.write_field_i32("ls6_1", "routine", 3)
    req = stub.write_calls[0]
    assert req.value.WhichOneof("kind") == "v_i32"
    assert req.value.v_i32 == 3


def test_write_field_bytes_sets_v_bytes_variant():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    client.write_field_bytes("ls6_1", "comment", b"hi")
    req = stub.write_calls[0]
    assert req.value.WhichOneof("kind") == "v_bytes"
    assert req.value.v_bytes == b"hi"


# ---------------------------------------------------------------------------
# read_field_* decodes the matching Value variant
# ---------------------------------------------------------------------------


def test_read_field_f32_returns_v_f32_value():
    stub = _FakeStub()
    stub.read_response = motion_pb2.ReadFieldResponse(
        ok=True, value=motion_pb2.Value(v_f32=42.5)
    )
    client = _client_with_stub(stub)
    assert client.read_field_f32("ls6_1", "cur_x") == pytest.approx(42.5)


def test_read_field_bool_returns_v_bool_value():
    stub = _FakeStub()
    stub.read_response = motion_pb2.ReadFieldResponse(
        ok=True, value=motion_pb2.Value(v_bool=True)
    )
    client = _client_with_stub(stub)
    assert client.read_field_bool("ls6_1", "done") is True


def test_read_field_type_mismatch_raises():
    """If runtime returned v_i32 but caller asked for f32, raise."""
    stub = _FakeStub()
    stub.read_response = motion_pb2.ReadFieldResponse(
        ok=True, value=motion_pb2.Value(v_i32=99)
    )
    client = _client_with_stub(stub)
    with pytest.raises(RuntimeFieldError, match="type mismatch"):
        client.read_field_f32("ls6_1", "cur_x")


# ---------------------------------------------------------------------------
# Field-level error
# ---------------------------------------------------------------------------


def test_write_field_field_error_raises_runtime_field_error():
    stub = _FakeStub()
    stub.write_response = motion_pb2.WriteFieldResponse(ok=False, error="unknown field")
    client = _client_with_stub(stub)
    with pytest.raises(RuntimeFieldError) as exc_info:
        client.write_field_f32("ls6_1", "nope", 1.0)
    assert exc_info.value.device == "ls6_1"
    assert exc_info.value.field == "nope"
    assert "unknown" in exc_info.value.reason


def test_read_field_field_error_raises_runtime_field_error():
    stub = _FakeStub()
    stub.read_response = motion_pb2.ReadFieldResponse(ok=False, error="slave offline")
    client = _client_with_stub(stub)
    with pytest.raises(RuntimeFieldError, match="slave offline"):
        client.read_field_bool("ls6_1", "done")


# ---------------------------------------------------------------------------
# Transport-level error translation
# ---------------------------------------------------------------------------


def test_unavailable_rpc_raises_runtime_connection_error():
    stub = _FakeStub()
    stub.write_response = lambda: (_ for _ in ()).throw(
        _FakeRpcError(grpc.StatusCode.UNAVAILABLE, "no route to host")
    )
    client = _client_with_stub(stub)
    with pytest.raises(RuntimeConnectionError, match="unreachable"):
        client.write_field_f32("ls6_1", "target_x", 1.0)


def test_deadline_exceeded_raises_runtime_timeout_error():
    stub = _FakeStub()
    stub.write_response = lambda: (_ for _ in ()).throw(
        _FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, "tick budget exceeded")
    )
    client = _client_with_stub(stub)
    with pytest.raises(RuntimeTimeoutError, match="timed out"):
        client.write_field_f32("ls6_1", "target_x", 1.0)


def test_read_unavailable_translates_to_runtime_connection_error():
    stub = _FakeStub()
    stub.read_response = lambda: (_ for _ in ()).throw(
        _FakeRpcError(grpc.StatusCode.UNAVAILABLE, "dropped")
    )
    client = _client_with_stub(stub)
    with pytest.raises(RuntimeConnectionError):
        client.read_field_bool("ls6_1", "done")


# ---------------------------------------------------------------------------
# Context manager closes the channel
# ---------------------------------------------------------------------------


def test_context_manager_closes_channel():
    closes: list[bool] = []

    class _FakeChannel:
        def close(self) -> None:
            closes.append(True)

    client = RuntimeClient(address="ignored")
    client._channel = _FakeChannel()  # type: ignore[assignment]
    with client:
        pass
    assert closes == [True]
