from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Sequence

from autoweaver.device.arm.base import GoalId

if TYPE_CHECKING:
    from autoweaver.motion_policy.world_board import WorldBoard


_HOME_POSE: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
_HOME_JOINT: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


class MockArm:
    """In-memory arm for tests and dry runs.

    Behavior:
      - ``move_j`` / ``move_l`` instantly "complete" the move by jumping
        the simulated pose / joint to the target after a configurable
        delay (default 0 — completes on the next feedback tick).
      - ``halt`` clears the in-flight goal and freezes pose / joint at
        the current value.
      - The feedback thread publishes pose / joint / running / enabled /
        error / safety_state / current_cmd_id at ``feedback_hz``.

    All control calls are recorded in ``self.calls`` so tests can assert
    on the sequence of interactions without spying.
    """

    def __init__(
        self,
        name: str,
        feedback_hz: float = 100.0,
        move_duration: float = 0.0,
    ):
        self.name = name
        self._feedback_period = 1.0 / feedback_hz
        self._move_duration = move_duration

        self.calls: list[tuple] = []

        self._goal_counter: GoalId = 0
        self._current_goal_id: GoalId | None = None

        self._pose: tuple[float, ...] = _HOME_POSE
        self._joint: tuple[float, ...] = _HOME_JOINT
        self._running: bool = False
        self._enabled: bool = True
        self._error: bool = False
        self._safety_state: int = 0  # 0 == ok

        self._goal_target: tuple[float, ...] | None = None
        self._goal_kind: str | None = None  # "j" or "l"
        self._goal_started_at: float = 0.0

        self._board: WorldBoard | None = None
        self._stop_flag = threading.Event()
        self._fb_thread: threading.Thread | None = None
        self._lock = threading.Lock()

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

    # --- feedback ---

    def register_outputs(self, board: WorldBoard) -> None:
        self._board = board
        prefix = self.name
        board.register(f"{prefix}.pose", tuple, writer=self.name)
        board.register(f"{prefix}.joint", tuple, writer=self.name)
        board.register(f"{prefix}.running", bool, writer=self.name)
        board.register(f"{prefix}.enabled", bool, writer=self.name)
        board.register(f"{prefix}.error", bool, writer=self.name)
        board.register(f"{prefix}.safety_state", int, writer=self.name)
        board.register(f"{prefix}.current_cmd_id", int, writer=self.name)

    # --- lifecycle ---

    def start(self) -> None:
        if self._board is None:
            raise RuntimeError(
                f"call {self.name}.register_outputs(board) before start()"
            )
        if self._fb_thread is not None and self._fb_thread.is_alive():
            return
        self._stop_flag.clear()
        self._fb_thread = threading.Thread(
            target=self._feedback_loop,
            daemon=True,
            name=f"{self.name}-feedback",
        )
        self._fb_thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        if self._fb_thread is not None:
            self._fb_thread.join(timeout=2.0)
            self._fb_thread = None

    def _feedback_loop(self) -> None:
        while not self._stop_flag.is_set():
            self._tick()
            time.sleep(self._feedback_period)

    def _tick(self) -> None:
        with self._lock:
            if self._goal_target is not None:
                elapsed = time.monotonic() - self._goal_started_at
                if elapsed >= self._move_duration:
                    if self._goal_kind == "j":
                        self._joint = self._goal_target
                    else:
                        self._pose = self._goal_target
                    self._goal_target = None
                    self._goal_kind = None
                    self._running = False
            pose = self._pose
            joint = self._joint
            running = self._running
            enabled = self._enabled
            error = self._error
            safety = self._safety_state
            cmd_id = self._current_goal_id or 0

        board = self._board
        if board is None:
            return
        prefix = self.name
        board.write(f"{prefix}.pose", pose, writer=self.name)
        board.write(f"{prefix}.joint", joint, writer=self.name)
        board.write(f"{prefix}.running", running, writer=self.name)
        board.write(f"{prefix}.enabled", enabled, writer=self.name)
        board.write(f"{prefix}.error", error, writer=self.name)
        board.write(f"{prefix}.safety_state", safety, writer=self.name)
        board.write(f"{prefix}.current_cmd_id", cmd_id, writer=self.name)

    # --- test helpers ---

    def publish_once(self) -> None:
        """Run a single feedback tick synchronously. Useful in tests."""
        self._tick()
