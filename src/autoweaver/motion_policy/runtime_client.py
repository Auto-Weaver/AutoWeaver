"""gRPC client for the Rust motion-runtime (0.8.0 goal service layer).

Python-side surface: submit a goal, poll status. The runtime owns the
field-level write/trigger/done handshake plus EtherCAT byte translation;
callers never deal with field names, byte offsets, or trigger edges.

Two device shapes:

  - SCARA (4-DOF, e.g. Epson LS6) — ``submit_scara_goal`` / ``read_scara_status``
  - Generic 6-DOF (reserved)     — ``submit_arm6_goal`` / ``read_arm6_status``

The 4-DOF / 6-DOF split is encoded in the proto messages themselves so
pyright catches dimension mismatches at the call site — there is no
``repeated float target`` with runtime-checked length.

Builder usage:

    with RuntimeClient("localhost:50051") as client:
        (client.scara_goal("ls6_1")
            .linear(x=100.0, y=200.0, z=50.0, u=0.0)
            .speed(50).accel(200)
            .submit())
        # ... later ...
        status = client.read_scara_status("ls6_1")
        if status.done:
            ...

Submit is non-blocking — the runtime starts the handshake and returns;
callers poll status. This matches the "submit + poll" pattern that BT
leaves' ``on_running`` already uses.

Three exception classes:

  - ``RuntimeConnectionError`` — gRPC channel unreachable
  - ``RuntimeTimeoutError``     — RPC exceeded its deadline
  - ``GoalError``               — runtime rejected the goal (unknown device,
                                  unsupported motion type, etc.)
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
    "GoalError",
    "ScaraGoalBuilder",
    "Arm6GoalBuilder",
]


class RuntimeConnectionError(RuntimeError):
    """gRPC channel cannot reach motion-runtime (runtime down, network unreachable)."""


class RuntimeTimeoutError(RuntimeError):
    """An RPC exceeded its deadline."""


class GoalError(RuntimeError):
    """motion-runtime rejected the goal.

    Carries the device name and the runtime's error string — typical
    causes are "unknown device", "unsupported motion type for this
    device", or "slave offline".
    """

    def __init__(self, device: str, reason: str):
        self.device = device
        self.reason = reason
        super().__init__(f"{device}: {reason}")


_DEFAULT_TIMEOUT_S = 1.0


class RuntimeClient:
    """Synchronous gRPC client for motion-runtime."""

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

    # --- goal builders ---

    def scara_goal(self, device: str) -> "ScaraGoalBuilder":
        return ScaraGoalBuilder(self, device)

    def arm6_goal(self, device: str) -> "Arm6GoalBuilder":
        return Arm6GoalBuilder(self, device)

    # --- status reads ---

    def read_scara_status(self, device: str) -> motion_pb2.ScaraStatusResponse:
        req = motion_pb2.StatusRequest(device=device)
        try:
            resp = self._stub.ReadScaraStatus(req, timeout=self._timeout_s)
        except grpc.RpcError as e:
            raise self._translate_rpc_error(e) from e
        if not resp.ok:
            raise GoalError(device, resp.error)
        return resp

    def read_arm6_status(self, device: str) -> motion_pb2.Arm6StatusResponse:
        req = motion_pb2.StatusRequest(device=device)
        try:
            resp = self._stub.ReadArm6Status(req, timeout=self._timeout_s)
        except grpc.RpcError as e:
            raise self._translate_rpc_error(e) from e
        if not resp.ok:
            raise GoalError(device, resp.error)
        return resp

    # --- internals ---

    def _submit_scara(self, goal: motion_pb2.ScaraGoal) -> None:
        try:
            resp = self._stub.SubmitScaraGoal(goal, timeout=self._timeout_s)
        except grpc.RpcError as e:
            raise self._translate_rpc_error(e) from e
        if not resp.ok:
            raise GoalError(goal.device, resp.error)

    def _submit_arm6(self, goal: motion_pb2.Arm6Goal) -> None:
        try:
            resp = self._stub.SubmitArm6Goal(goal, timeout=self._timeout_s)
        except grpc.RpcError as e:
            raise self._translate_rpc_error(e) from e
        if not resp.ok:
            raise GoalError(goal.device, resp.error)

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


class ScaraGoalBuilder:
    """Chainable builder for a SCARA (4-DOF) goal.

    Motion type is set by one of the motion-shaped setters (``linear`` /
    ``go`` / ``jump`` / ``home``); the call also takes the target
    coordinates. ``speed`` / ``accel`` are independent setters that can
    be chained in any order. ``submit`` sends the RPC.

    HOME has no target — the runtime ignores x/y/z/u for HOME, but the
    builder still accepts ``.home()`` with no args.
    """

    def __init__(self, client: RuntimeClient, device: str):
        self._client = client
        self._goal = motion_pb2.ScaraGoal(device=device)

    # --- motion setters ---

    def go(self, *, x: float, y: float, z: float, u: float) -> "ScaraGoalBuilder":
        self._goal.motion = motion_pb2.MOTION4_GO
        self._set_target(x, y, z, u)
        return self

    def jump(self, *, x: float, y: float, z: float, u: float) -> "ScaraGoalBuilder":
        self._goal.motion = motion_pb2.MOTION4_JUMP
        self._set_target(x, y, z, u)
        return self

    def linear(self, *, x: float, y: float, z: float, u: float) -> "ScaraGoalBuilder":
        self._goal.motion = motion_pb2.MOTION4_LINEAR
        self._set_target(x, y, z, u)
        return self

    def home(self) -> "ScaraGoalBuilder":
        self._goal.motion = motion_pb2.MOTION4_HOME
        return self

    # --- parameter setters ---

    def speed(self, value: int) -> "ScaraGoalBuilder":
        self._goal.speed = value
        return self

    def accel(self, value: int) -> "ScaraGoalBuilder":
        self._goal.accel = value
        return self

    # --- submit ---

    def submit(self) -> None:
        if self._goal.motion == motion_pb2.MOTION4_UNSPECIFIED:
            raise GoalError(
                self._goal.device,
                "no motion type set — call .go() / .jump() / .linear() / .home() before submit()",
            )
        self._client._submit_scara(self._goal)

    # --- internals ---

    def _set_target(self, x: float, y: float, z: float, u: float) -> None:
        self._goal.x = x
        self._goal.y = y
        self._goal.z = z
        self._goal.u = u


class Arm6GoalBuilder:
    """Chainable builder for a 6-DOF goal. Mirrors ScaraGoalBuilder."""

    def __init__(self, client: RuntimeClient, device: str):
        self._client = client
        self._goal = motion_pb2.Arm6Goal(device=device)

    # --- motion setters ---

    def go(
        self, *, x: float, y: float, z: float, rx: float, ry: float, rz: float
    ) -> "Arm6GoalBuilder":
        self._goal.motion = motion_pb2.MOTION6_GO
        self._set_target(x, y, z, rx, ry, rz)
        return self

    def linear(
        self, *, x: float, y: float, z: float, rx: float, ry: float, rz: float
    ) -> "Arm6GoalBuilder":
        self._goal.motion = motion_pb2.MOTION6_LINEAR
        self._set_target(x, y, z, rx, ry, rz)
        return self

    def home(self) -> "Arm6GoalBuilder":
        self._goal.motion = motion_pb2.MOTION6_HOME
        return self

    # --- parameter setters ---

    def speed(self, value: int) -> "Arm6GoalBuilder":
        self._goal.speed = value
        return self

    def accel(self, value: int) -> "Arm6GoalBuilder":
        self._goal.accel = value
        return self

    # --- submit ---

    def submit(self) -> None:
        if self._goal.motion == motion_pb2.MOTION6_UNSPECIFIED:
            raise GoalError(
                self._goal.device,
                "no motion type set — call .go() / .linear() / .home() before submit()",
            )
        self._client._submit_arm6(self._goal)

    # --- internals ---

    def _set_target(
        self, x: float, y: float, z: float, rx: float, ry: float, rz: float
    ) -> None:
        self._goal.x = x
        self._goal.y = y
        self._goal.z = z
        self._goal.rx = rx
        self._goal.ry = ry
        self._goal.rz = rz
