"""In-memory mock of ``RuntimeClient`` for driver tests.

Mirrors the public surface of ``RuntimeClient`` exactly so a driver
(e.g. ``EpsonLS6``) under test can swap one for the other.

Behavior:

  - Each goal submission appends to ``self.goals`` as a tuple of
    ``(kind, device, motion_name, fields_dict)``. Drivers' tests can
    assert "the third call to submit was a LINEAR to (100, 200, 50, 0)".
  - The mock keeps a per-device status dict that ``read_*_status``
    returns. After a goal submission the mock sets ``done=False`` /
    ``busy=True``; tests use ``preload_status`` to flip those, or call
    ``complete_last_goal`` to simulate the runtime finishing the move.
  - ``preload_status(device, **kwargs)`` and ``preload_pose(device, ...)``
    seed state without recording a call.

This is a test double, not a runtime artifact — drivers depend on the
``RuntimeClient`` shape, not on this specific class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Type

from autoweaver.motion_policy.runtime_client import (
    Arm6GoalBuilder,
    GoalError,
    RuntimeClient,
    ScaraGoalBuilder,
)


__all__ = [
    "MockRuntimeClient",
    "MockScaraGoalBuilder",
    "MockArm6GoalBuilder",
    "MockScaraStatus",
    "MockArm6Status",
]


@dataclass
class MockScaraStatus:
    ok: bool = True
    error: str = ""
    done: bool = False
    busy: bool = False
    error_code: int = 0
    current_x: float = 0.0
    current_y: float = 0.0
    current_z: float = 0.0
    current_u: float = 0.0
    joint_1: float = 0.0
    joint_2: float = 0.0
    joint_3: float = 0.0
    joint_4: float = 0.0


@dataclass
class MockArm6Status:
    ok: bool = True
    error: str = ""
    done: bool = False
    busy: bool = False
    error_code: int = 0
    current_x: float = 0.0
    current_y: float = 0.0
    current_z: float = 0.0
    current_rx: float = 0.0
    current_ry: float = 0.0
    current_rz: float = 0.0
    joint_1: float = 0.0
    joint_2: float = 0.0
    joint_3: float = 0.0
    joint_4: float = 0.0
    joint_5: float = 0.0
    joint_6: float = 0.0


@dataclass
class _DeviceState:
    scara_status: MockScaraStatus = field(default_factory=MockScaraStatus)
    arm6_status: MockArm6Status = field(default_factory=MockArm6Status)


class MockRuntimeClient:
    """In-memory stand-in for ``RuntimeClient``.

    Drivers under test call ``client.scara_goal("ls6_1").linear(...).submit()``
    exactly as they would against the real client; the mock records the
    submission and updates the device's status to ``busy=True, done=False``.
    Tests then call ``complete_last_goal("ls6_1")`` to simulate the
    runtime finishing the handshake, after which ``read_scara_status``
    returns ``done=True``.
    """

    def __init__(self) -> None:
        self._states: dict[str, _DeviceState] = {}
        # Each entry: ("scara"|"arm6", device, motion_name, fields_dict)
        self.goals: list[tuple[str, str, str, dict[str, Any]]] = []

    # --- lifecycle ---

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

    # --- goal builders ---

    def scara_goal(self, device: str) -> "MockScaraGoalBuilder":
        return MockScaraGoalBuilder(self, device)

    def arm6_goal(self, device: str) -> "MockArm6GoalBuilder":
        return MockArm6GoalBuilder(self, device)

    # --- status reads ---

    def read_scara_status(self, device: str) -> MockScaraStatus:
        state = self._states.get(device)
        if state is None:
            raise GoalError(device, "unknown device")
        return state.scara_status

    def read_arm6_status(self, device: str) -> MockArm6Status:
        state = self._states.get(device)
        if state is None:
            raise GoalError(device, "unknown device")
        return state.arm6_status

    # --- test helpers ---

    def preload_scara_status(self, device: str, **kwargs: Any) -> None:
        """Seed a SCARA device's status fields without recording a goal."""
        state = self._states.setdefault(device, _DeviceState())
        for k, v in kwargs.items():
            setattr(state.scara_status, k, v)

    def preload_arm6_status(self, device: str, **kwargs: Any) -> None:
        """Seed a 6-DOF device's status fields without recording a goal."""
        state = self._states.setdefault(device, _DeviceState())
        for k, v in kwargs.items():
            setattr(state.arm6_status, k, v)

    def complete_last_goal(self, device: str) -> None:
        """Simulate the runtime finishing whatever was last submitted: flip
        the device's status to ``done=True, busy=False``."""
        state = self._states.setdefault(device, _DeviceState())
        state.scara_status.done = True
        state.scara_status.busy = False
        state.arm6_status.done = True
        state.arm6_status.busy = False

    # --- internals used by builders ---

    def _record_scara(self, device: str, motion_name: str, fields: dict[str, Any]) -> None:
        self.goals.append(("scara", device, motion_name, fields))
        state = self._states.setdefault(device, _DeviceState())
        state.scara_status.done = False
        state.scara_status.busy = True
        state.scara_status.error_code = 0

    def _record_arm6(self, device: str, motion_name: str, fields: dict[str, Any]) -> None:
        self.goals.append(("arm6", device, motion_name, fields))
        state = self._states.setdefault(device, _DeviceState())
        state.arm6_status.done = False
        state.arm6_status.busy = True
        state.arm6_status.error_code = 0


class MockScaraGoalBuilder:
    """Mirror of ``ScaraGoalBuilder`` writing into an in-memory store."""

    def __init__(self, client: MockRuntimeClient, device: str):
        self._client = client
        self._device = device
        self._motion: str | None = None
        self._fields: dict[str, Any] = {}

    def go(self, *, x: float, y: float, z: float, u: float) -> "MockScaraGoalBuilder":
        self._motion = "GO"
        self._fields.update({"x": x, "y": y, "z": z, "u": u})
        return self

    def jump(self, *, x: float, y: float, z: float, u: float) -> "MockScaraGoalBuilder":
        self._motion = "JUMP"
        self._fields.update({"x": x, "y": y, "z": z, "u": u})
        return self

    def linear(self, *, x: float, y: float, z: float, u: float) -> "MockScaraGoalBuilder":
        self._motion = "LINEAR"
        self._fields.update({"x": x, "y": y, "z": z, "u": u})
        return self

    def home(self) -> "MockScaraGoalBuilder":
        self._motion = "HOME"
        return self

    def speed(self, value: int) -> "MockScaraGoalBuilder":
        self._fields["speed"] = value
        return self

    def accel(self, value: int) -> "MockScaraGoalBuilder":
        self._fields["accel"] = value
        return self

    def submit(self) -> None:
        if self._motion is None:
            raise GoalError(
                self._device,
                "no motion type set — call .go() / .jump() / .linear() / .home() before submit()",
            )
        self._client._record_scara(self._device, self._motion, dict(self._fields))


class MockArm6GoalBuilder:
    """Mirror of ``Arm6GoalBuilder`` writing into an in-memory store."""

    def __init__(self, client: MockRuntimeClient, device: str):
        self._client = client
        self._device = device
        self._motion: str | None = None
        self._fields: dict[str, Any] = {}

    def go(
        self, *, x: float, y: float, z: float, rx: float, ry: float, rz: float
    ) -> "MockArm6GoalBuilder":
        self._motion = "GO"
        self._fields.update({"x": x, "y": y, "z": z, "rx": rx, "ry": ry, "rz": rz})
        return self

    def linear(
        self, *, x: float, y: float, z: float, rx: float, ry: float, rz: float
    ) -> "MockArm6GoalBuilder":
        self._motion = "LINEAR"
        self._fields.update({"x": x, "y": y, "z": z, "rx": rx, "ry": ry, "rz": rz})
        return self

    def home(self) -> "MockArm6GoalBuilder":
        self._motion = "HOME"
        return self

    def speed(self, value: int) -> "MockArm6GoalBuilder":
        self._fields["speed"] = value
        return self

    def accel(self, value: int) -> "MockArm6GoalBuilder":
        self._fields["accel"] = value
        return self

    def submit(self) -> None:
        if self._motion is None:
            raise GoalError(
                self._device,
                "no motion type set — call .go() / .linear() / .home() before submit()",
            )
        self._client._record_arm6(self._device, self._motion, dict(self._fields))


# Mypy/pyright structural compatibility: ensure mock builders are usable
# anywhere the real ones are expected. (Drivers that type their dep as
# RuntimeClient still work — duck typing — but this comment is here so
# future maintainers know the shapes are deliberately mirrored.)
_ = (RuntimeClient, ScaraGoalBuilder, Arm6GoalBuilder)
