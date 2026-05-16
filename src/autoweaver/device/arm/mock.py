from __future__ import annotations

import threading
import time
from typing import Sequence

import numpy as np

from autoweaver.device.arm.base import GoalId
from autoweaver.geometry import transforms


_HOME_POSE: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
_HOME_JOINT: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

# MockArm mirrors Dobot's pose convention so leaves see the same matrix
# shape whether they're talking to a mock or real hardware. See NEXT-008.
_POSE_RPY_CONVENTION = "zyx_intrinsic_deg"


class MockArm:
    """In-memory arm for tests and dry runs (NEXT-008 pull model).

    Behavior:
      - ``move_j`` / ``move_l`` "complete" the move by jumping the
        simulated pose / joint to the target after ``move_duration``
        seconds (default 0 — completes immediately).
      - ``halt`` clears the in-flight goal and freezes pose / joint at
        the current value.
      - ``get_flange_pose()`` returns a 4×4 matrix derived from the
        current simulated pose (same convention as Dobot SDK).

    All control calls are recorded in ``self.calls`` so tests can assert
    on the sequence of interactions without spying.
    """

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
        self._goal_kind: str | None = None  # "j" or "l"
        self._goal_started_at: float = 0.0

        self._lock = threading.Lock()
        self._started = False

    # --- control ---

    def move_j(self, target: Sequence[float]) -> GoalId:
        return self._start_goal("j", target)

    def move_l(self, target: Sequence[float]) -> GoalId:
        return self._start_goal("l", target)

    def halt(self, goal_id: GoalId) -> None:
        with self._lock:
            self.calls.append(("halt", goal_id))
            if goal_id != self._current_goal_id:
                return
            self._goal_target = None
            self._goal_kind = None
            self._current_goal_id = None
            self._running = False

    def _start_goal(self, kind: str, target: Sequence[float]) -> GoalId:
        target_tuple = tuple(float(x) for x in target)
        if len(target_tuple) != 6:
            raise ValueError(
                f"target must have 6 elements, got {len(target_tuple)}"
            )
        if not self._started:
            raise RuntimeError(f"call {self.name}.start() before issuing move commands")
        with self._lock:
            self._goal_counter += 1
            gid = self._goal_counter
            self._current_goal_id = gid
            self._goal_target = target_tuple
            self._goal_kind = kind
            self._goal_started_at = time.monotonic()
            self._running = True
            self.calls.append((f"move_{kind}", gid, target_tuple))
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
        """If the in-flight goal's duration has elapsed, snap pose/joint
        to the target. Driven on-demand by readers — no background thread.
        """
        with self._lock:
            if self._goal_target is None:
                return
            elapsed = time.monotonic() - self._goal_started_at
            if elapsed < self._move_duration:
                return
            if self._goal_kind == "j":
                self._joint = self._goal_target
            else:
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
