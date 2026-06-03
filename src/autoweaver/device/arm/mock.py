from __future__ import annotations

import threading
import time
from typing import Sequence

import numpy as np

from autoweaver.device.arm.base import (
    GoalId,
    validate_joint_target,
    validate_target_6dof,
)
from autoweaver.frames import transforms


_HOME_POSE: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
_HOME_JOINT: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

# MockArm mirrors Dobot's pose convention so leaves see the same matrix
# shape whether they're talking to a mock or real hardware. See NEXT-008.
_POSE_RPY_CONVENTION = "zyx_intrinsic_deg"


class MockArm:
    """In-memory 6-DOF arm for tests and dry runs. Conforms to ArmBase6.

    Behavior:
      - ``move_j`` / ``move_l`` take a 6-tuple Cartesian target and
        "complete" by jumping ``_pose`` to it after ``move_duration`` seconds.
      - ``move_j_joints`` takes a 6-element joint-angle target and jumps
        ``_joint``.
      - ``halt`` clears the in-flight goal and freezes pose / joint at
        the current value.
      - ``get_flange_pose()`` returns a 4×4 matrix derived from the
        current simulated pose (same convention as Dobot SDK).

    The mock does not do IK or FK — ``move_j(cartesian)`` does not update
    ``_joint``, and ``move_j_joints(joints)`` does not update ``_pose``.
    Tests that need to assert on the other-axis state should call
    ``set_pose()`` directly.

    All control calls are recorded in ``self.calls`` so tests can assert
    on the sequence of interactions without spying.

    For 4-DOF SCARA testing use ``EpsonLS6`` with ``MockRuntimeClient``;
    there is no parameterized 4-DOF mock because the SCARA shape diverges
    enough (4-tuple targets, ``jump`` method, no ``move_j_joints``) that
    a dedicated path is clearer than a dof flag.
    """

    dof = 6

    def __init__(
        self,
        name: str,
        move_duration: float = 0.0,
    ):
        self.name = name
        self._move_duration = move_duration

        self.calls: list[tuple] = []

        self._goal_counter: GoalId = 0
        self._current_goal_id: GoalId | None = None

        self._pose: tuple[float, ...] = _HOME_POSE
        self._joint: tuple[float, ...] = _HOME_JOINT
        self._running: bool = False

        self._goal_target: tuple[float, ...] | None = None
        self._goal_kind: str | None = None  # "j" | "l" | "j_joints"
        self._goal_started_at: float = 0.0

        self._lock = threading.Lock()
        self._started = False

    # --- control ---

    def move_j(self, target: Sequence[float]) -> GoalId:
        target = validate_target_6dof(target, self.name)
        return self._start_goal("j", target)

    def move_l(self, target: Sequence[float]) -> GoalId:
        target = validate_target_6dof(target, self.name)
        return self._start_goal("l", target)

    def move_j_joints(self, target: Sequence[float]) -> GoalId:
        target = validate_joint_target(target, self.dof, self.name)
        return self._start_goal("j_joints", target)

    def halt(self, goal_id: GoalId) -> None:
        with self._lock:
            self.calls.append(("halt", goal_id))
            if goal_id != self._current_goal_id:
                return
            self._goal_target = None
            self._goal_kind = None
            self._current_goal_id = None
            self._running = False

    def _start_goal(self, kind: str, target: tuple[float, ...]) -> GoalId:
        # target is already validated at the public entry point.
        if not self._started:
            raise RuntimeError(f"call {self.name}.start() before issuing move commands")
        with self._lock:
            self._goal_counter += 1
            gid = self._goal_counter
            self._current_goal_id = gid
            self._goal_target = target
            self._goal_kind = kind
            self._goal_started_at = time.monotonic()
            self._running = True
            self.calls.append((f"move_{kind}", gid, target))
            return gid

    # --- feedback (pull) ---

    def get_flange_pose(self) -> np.ndarray:
        if not self._started:
            raise RuntimeError(f"call {self.name}.start() before reading pose")
        self._advance_goal()
        with self._lock:
            pose = self._pose
        return transforms.euler_to_matrix(
            np.array(pose[:3], dtype=np.float64),
            pose[3:],
            _POSE_RPY_CONVENTION,
        )

    # --- lifecycle ---

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    # --- internal goal simulation ---

    def _advance_goal(self) -> None:
        """If the in-flight goal's duration has elapsed, snap pose or joint
        to the target. Driven on-demand by readers — no background thread.
        """
        with self._lock:
            if self._goal_target is None:
                return
            elapsed = time.monotonic() - self._goal_started_at
            if elapsed < self._move_duration:
                return
            if self._goal_kind == "j_joints":
                self._joint = self._goal_target
            else:
                # "j" and "l" both have Cartesian targets.
                self._pose = self._goal_target
            self._goal_target = None
            self._goal_kind = None
            self._running = False

    # --- test helpers ---

    def set_pose(self, pose: Sequence[float]) -> None:
        """Force the simulated pose to a specific value. For test setup only."""
        pose_tuple = tuple(float(x) for x in pose)
        if len(pose_tuple) != 6:
            raise ValueError(f"pose must have 6 elements, got {len(pose_tuple)}")
        with self._lock:
            self._pose = pose_tuple
