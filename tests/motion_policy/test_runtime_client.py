from __future__ import annotations

from typing import Callable

import grpc
import pytest

from autoweaver.motion_policy._proto import motion_pb2
from autoweaver.motion_policy.runtime_client import (
    GoalError,
    RuntimeClient,
    RuntimeConnectionError,
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

    Each response field can be either a response message or a
    zero-argument callable that produces one (or raises) — the latter
    lets a test simulate transport errors.
    """

    def __init__(self):
        self.scara_goal_calls: list[motion_pb2.ScaraGoal] = []
        self.arm6_goal_calls: list[motion_pb2.Arm6Goal] = []
        self.scara_status_calls: list[motion_pb2.StatusRequest] = []
        self.arm6_status_calls: list[motion_pb2.StatusRequest] = []

        self.scara_goal_response: (
            motion_pb2.GoalResponse | Callable[[], motion_pb2.GoalResponse]
        ) = motion_pb2.GoalResponse(ok=True)
        self.arm6_goal_response: (
            motion_pb2.GoalResponse | Callable[[], motion_pb2.GoalResponse]
        ) = motion_pb2.GoalResponse(ok=True)
        self.scara_status_response: (
            motion_pb2.ScaraStatusResponse
            | Callable[[], motion_pb2.ScaraStatusResponse]
            | None
        ) = None
        self.arm6_status_response: (
            motion_pb2.Arm6StatusResponse
            | Callable[[], motion_pb2.Arm6StatusResponse]
            | None
        ) = None

    def SubmitScaraGoal(self, req, timeout=None):
        self.scara_goal_calls.append(req)
        if callable(self.scara_goal_response):
            return self.scara_goal_response()
        return self.scara_goal_response

    def SubmitArm6Goal(self, req, timeout=None):
        self.arm6_goal_calls.append(req)
        if callable(self.arm6_goal_response):
            return self.arm6_goal_response()
        return self.arm6_goal_response

    def ReadScaraStatus(self, req, timeout=None):
        self.scara_status_calls.append(req)
        if self.scara_status_response is None:
            raise AssertionError("test forgot to set scara_status_response")
        if callable(self.scara_status_response):
            return self.scara_status_response()
        return self.scara_status_response

    def ReadArm6Status(self, req, timeout=None):
        self.arm6_status_calls.append(req)
        if self.arm6_status_response is None:
            raise AssertionError("test forgot to set arm6_status_response")
        if callable(self.arm6_status_response):
            return self.arm6_status_response()
        return self.arm6_status_response


def _client_with_stub(stub: _FakeStub) -> RuntimeClient:
    """Construct a RuntimeClient and swap its stub for the fake."""
    client = RuntimeClient(address="ignored")
    client._stub = stub  # type: ignore[assignment]
    return client


# ---------------------------------------------------------------------------
# SCARA submit — motion encoding
# ---------------------------------------------------------------------------


def test_scara_linear_encodes_motion4_linear():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    (
        client.scara_goal("ls6_1")
        .linear(x=100.5, y=200.0, z=50.0, u=0.0)
        .speed(50)
        .accel(200)
        .submit()
    )
    assert len(stub.scara_goal_calls) == 1
    goal = stub.scara_goal_calls[0]
    assert goal.device == "ls6_1"
    assert goal.motion == motion_pb2.MOTION4_LINEAR
    assert goal.x == pytest.approx(100.5)
    assert goal.y == pytest.approx(200.0)
    assert goal.z == pytest.approx(50.0)
    assert goal.u == pytest.approx(0.0)
    assert goal.speed == 50
    assert goal.accel == 200


def test_scara_go_encodes_motion4_go():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    client.scara_goal("ls6_1").go(x=1.0, y=2.0, z=3.0, u=4.0).submit()
    assert stub.scara_goal_calls[0].motion == motion_pb2.MOTION4_GO


def test_scara_jump_encodes_motion4_jump():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    client.scara_goal("ls6_1").jump(x=1.0, y=2.0, z=3.0, u=4.0).submit()
    assert stub.scara_goal_calls[0].motion == motion_pb2.MOTION4_JUMP


def test_scara_home_encodes_motion4_home_and_ignores_target():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    client.scara_goal("ls6_1").home().submit()
    goal = stub.scara_goal_calls[0]
    assert goal.motion == motion_pb2.MOTION4_HOME
    # No target set — proto default is 0.0 for unset float fields.
    assert goal.x == 0.0 and goal.y == 0.0 and goal.z == 0.0 and goal.u == 0.0


def test_scara_submit_without_motion_raises():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    with pytest.raises(GoalError, match="no motion type set"):
        client.scara_goal("ls6_1").speed(50).submit()
    assert stub.scara_goal_calls == []


# ---------------------------------------------------------------------------
# SCARA submit — error paths
# ---------------------------------------------------------------------------


def test_scara_goal_error_response_raises_goal_error():
    stub = _FakeStub()
    stub.scara_goal_response = motion_pb2.GoalResponse(
        ok=False, error="unsupported motion type"
    )
    client = _client_with_stub(stub)
    with pytest.raises(GoalError) as exc_info:
        client.scara_goal("ls6_1").linear(x=1.0, y=2.0, z=3.0, u=0.0).submit()
    assert exc_info.value.device == "ls6_1"
    assert "unsupported" in exc_info.value.reason


def test_scara_unavailable_translates_to_runtime_connection_error():
    stub = _FakeStub()
    stub.scara_goal_response = lambda: (_ for _ in ()).throw(
        _FakeRpcError(grpc.StatusCode.UNAVAILABLE, "no route to host")
    )
    client = _client_with_stub(stub)
    with pytest.raises(RuntimeConnectionError, match="unreachable"):
        client.scara_goal("ls6_1").linear(x=1.0, y=2.0, z=3.0, u=0.0).submit()


def test_scara_deadline_translates_to_runtime_timeout_error():
    stub = _FakeStub()
    stub.scara_goal_response = lambda: (_ for _ in ()).throw(
        _FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, "tick budget exceeded")
    )
    client = _client_with_stub(stub)
    with pytest.raises(RuntimeTimeoutError, match="timed out"):
        client.scara_goal("ls6_1").linear(x=1.0, y=2.0, z=3.0, u=0.0).submit()


# ---------------------------------------------------------------------------
# SCARA status read
# ---------------------------------------------------------------------------


def test_read_scara_status_returns_full_response():
    stub = _FakeStub()
    stub.scara_status_response = motion_pb2.ScaraStatusResponse(
        ok=True,
        done=True,
        busy=False,
        error_code=0,
        current_x=100.0,
        current_y=200.0,
        current_z=50.0,
        current_u=0.0,
    )
    client = _client_with_stub(stub)
    status = client.read_scara_status("ls6_1")
    assert status.done is True
    assert status.busy is False
    assert status.current_x == pytest.approx(100.0)


def test_read_scara_status_error_raises_goal_error():
    stub = _FakeStub()
    stub.scara_status_response = motion_pb2.ScaraStatusResponse(
        ok=False, error="unknown device"
    )
    client = _client_with_stub(stub)
    with pytest.raises(GoalError, match="unknown device"):
        client.read_scara_status("ls6_1")


def test_read_scara_status_unavailable_translates_to_runtime_connection_error():
    stub = _FakeStub()
    stub.scara_status_response = lambda: (_ for _ in ()).throw(
        _FakeRpcError(grpc.StatusCode.UNAVAILABLE, "dropped")
    )
    client = _client_with_stub(stub)
    with pytest.raises(RuntimeConnectionError):
        client.read_scara_status("ls6_1")


# ---------------------------------------------------------------------------
# 6-DOF — same shape as SCARA, sanity-check the encoding path
# ---------------------------------------------------------------------------


def test_arm6_linear_encodes_motion6_linear_with_all_six_components():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    (
        client.arm6_goal("nova_1")
        .linear(x=1.0, y=2.0, z=3.0, rx=10.0, ry=20.0, rz=30.0)
        .speed(60)
        .accel(150)
        .submit()
    )
    goal = stub.arm6_goal_calls[0]
    assert goal.motion == motion_pb2.MOTION6_LINEAR
    assert goal.x == pytest.approx(1.0)
    assert goal.rx == pytest.approx(10.0)
    assert goal.rz == pytest.approx(30.0)
    assert goal.speed == 60


def test_arm6_submit_without_motion_raises():
    stub = _FakeStub()
    client = _client_with_stub(stub)
    with pytest.raises(GoalError, match="no motion type set"):
        client.arm6_goal("nova_1").speed(50).submit()


def test_read_arm6_status_returns_response():
    stub = _FakeStub()
    stub.arm6_status_response = motion_pb2.Arm6StatusResponse(
        ok=True, done=True, busy=False, current_rz=90.0
    )
    client = _client_with_stub(stub)
    status = client.read_arm6_status("nova_1")
    assert status.done is True
    assert status.current_rz == pytest.approx(90.0)


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
