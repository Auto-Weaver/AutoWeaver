from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Sequence

from autoweaver.device.arm._dobot_sdk import (
    DobotApiDashboard,
    DobotApiFeedBack,
)
from autoweaver.device.arm.base import GoalId

if TYPE_CHECKING:
    from autoweaver.motion_policy.world_board import WorldBoard


logger = logging.getLogger(__name__)

DASHBOARD_PORT = 29999
FEEDBACK_PORT = 30004

# Dobot MovJ / MovL coordinateMode argument
COORD_POSE = 0   # Cartesian (X, Y, Z, Rx, Ry, Rz)
COORD_JOINT = 1  # Joint angles (J1, J2, J3, J4, J5, J6)


class Dobot:
    """Dobot Nova 5 / Nova 2 arm.

    Lifecycle:
        dobot1 = Dobot(ip="192.168.1.10", name="dobot1")
        dobot1.register_outputs(world_board)   # offline; declares keys
        dobot1.start()                         # connects + starts feedback thread
        ...                                    # use through ActionLeaf
        dobot1.stop()                          # closes feedback thread

    Construction is intentionally side-effect-free; sockets are opened in
    ``start()``. This keeps construction safe for tests and dry runs and
    matches the lifecycle every other ArmBase implementation follows.

    Move semantics (NEXT-006):
      - ``move_j`` / ``move_l`` are fire-and-forget at the task level.
        The TCP RPC blocks ~5-15ms waiting for the controller's ACK; the
        physical motion (hundreds of ms to seconds) is observed via the
        feedback stream, not by waiting on the RPC.
      - ``halt(goal_id)`` sends Stop() to the controller. Stale halts —
        where ``goal_id`` no longer matches the current goal — are
        ignored so a delayed halt cannot interrupt a newer goal.
    """

    def __init__(
        self,
        ip: str,
        name: str,
        joint_coord_mode: bool = True,
    ):
        self.name = name
        self.ip = ip
        self._joint_default = COORD_JOINT if joint_coord_mode else COORD_POSE

        self._dashboard: DobotApiDashboard | None = None
        self._feedback: DobotApiFeedBack | None = None

        self._goal_counter: GoalId = 0
        self._current_goal_id: GoalId | None = None

        self._stop_flag = threading.Event()
        self._fb_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._board: WorldBoard | None = None

    # --- control ---

    def move_j(self, target: Sequence[float]) -> GoalId:
        return self._send_move("j", target, self._joint_default)

    def move_l(self, target: Sequence[float]) -> GoalId:
        # MovL is Cartesian-only in practice — joint-space MovL doesn't
        # have a clean physical meaning. Force pose mode.
        return self._send_move("l", target, COORD_POSE)

    def halt(self, goal_id: GoalId) -> None:
        with self._lock:
            if goal_id != self._current_goal_id:
                return  # stale halt; do nothing
            if self._dashboard is None:
                self._current_goal_id = None
                return
            self._dashboard.Stop()
            self._current_goal_id = None

    def _send_move(
        self, kind: str, target: Sequence[float], coord_mode: int
    ) -> GoalId:
        target_tuple = tuple(float(x) for x in target)
        if len(target_tuple) != 6:
            raise ValueError(
                f"target must have 6 elements, got {len(target_tuple)}"
            )
        if self._dashboard is None:
            raise RuntimeError(
                f"call {self.name}.start() before issuing move commands"
            )
        with self._lock:
            self._goal_counter += 1
            gid = self._goal_counter
            self._current_goal_id = gid

        a, b, c, d, e, f = target_tuple
        if kind == "j":
            self._dashboard.MovJ(a, b, c, d, e, f, coord_mode)
        else:
            self._dashboard.MovL(a, b, c, d, e, f, coord_mode)
        return gid

    # --- feedback registration ---

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

        self._dashboard = DobotApiDashboard(self.ip, DASHBOARD_PORT)
        self._feedback = DobotApiFeedBack(self.ip, FEEDBACK_PORT)
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
        for sdk in (self._dashboard, self._feedback):
            if sdk is None:
                continue
            try:
                sdk.close()
            except Exception:  # noqa: BLE001 -- best-effort cleanup
                logger.exception("error closing %s socket on %s", sdk, self.name)
        self._dashboard = None
        self._feedback = None

    def _feedback_loop(self) -> None:
        assert self._feedback is not None
        while not self._stop_flag.is_set():
            try:
                frame = self._feedback.feedBackData()
            except Exception:  # noqa: BLE001 -- main flow lets the thread die
                logger.exception("feedback read failed on %s", self.name)
                return
            if frame is None or len(frame) == 0:
                continue
            self._publish(frame)

    def _publish(self, frame) -> None:
        board = self._board
        if board is None:
            return
        prefix = self.name
        f0 = frame[0]
        pose = tuple(float(v) for v in f0["ToolVectorActual"])
        joint = tuple(float(v) for v in f0["QActual"])
        running = bool(f0["RunningStatus"])
        enabled = bool(f0["EnableStatus"])
        error = bool(f0["ErrorStatus"])
        safety = int(f0["SafetyState"])
        cmd_id = int(f0["CurrentCommandId"])

        board.write(f"{prefix}.pose", pose, writer=self.name)
        board.write(f"{prefix}.joint", joint, writer=self.name)
        board.write(f"{prefix}.running", running, writer=self.name)
        board.write(f"{prefix}.enabled", enabled, writer=self.name)
        board.write(f"{prefix}.error", error, writer=self.name)
        board.write(f"{prefix}.safety_state", safety, writer=self.name)
        board.write(f"{prefix}.current_cmd_id", cmd_id, writer=self.name)
