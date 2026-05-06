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
from autoweaver.device.arm.dobot_states import (
    ROBOT_MODE_DISABLED,
    ROBOT_MODE_ENABLE,
    ROBOT_MODE_ERROR,
    ROBOT_MODE_INIT,
    ROBOT_MODE_NO_CONTROLLER,
    ROBOT_MODES_NEED_STOP,
    robot_mode_name,
)

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

    def move_j(self, target: Sequence[float], speed: int | None = None) -> GoalId:
        return self._send_move("j", target, self._joint_default, speed=speed)

    def move_l(self, target: Sequence[float], speed: int | None = None) -> GoalId:
        # MovL is Cartesian-only in practice — joint-space MovL doesn't
        # have a clean physical meaning. Force pose mode.
        return self._send_move("l", target, COORD_POSE, speed=speed)

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
        self,
        kind: str,
        target: Sequence[float],
        coord_mode: int,
        speed: int | None = None,
    ) -> GoalId:
        target_tuple = tuple(float(x) for x in target)
        if len(target_tuple) != 6:
            raise ValueError(
                f"target must have 6 elements, got {len(target_tuple)}"
            )
        if speed is not None and not (1 <= speed <= 100):
            raise ValueError(f"speed must be in [1, 100], got {speed}")
        if self._dashboard is None:
            raise RuntimeError(
                f"call {self.name}.start() before issuing move commands"
            )
        with self._lock:
            self._goal_counter += 1
            gid = self._goal_counter
            self._current_goal_id = gid

        a, b, c, d, e, f = target_tuple
        v = speed if speed is not None else -1   # SDK convention: -1 means "omit"
        if kind == "j":
            self._dashboard.MovJ(a, b, c, d, e, f, coord_mode, v=v)
        else:
            self._dashboard.MovL(a, b, c, d, e, f, coord_mode, v=v)
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
        board.register(f"{prefix}.robot_mode", int, writer=self.name)

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

    def acquire_control(
        self,
        timeout_s: float = 15.0,
        poll_interval_s: float = 0.2,
    ) -> None:
        """Take TCP/IP control of the controller and bring it to ENABLED state.

        Empirical sequence (the PDF's "modes that allow RequestControl" table
        is stricter than the controller actually enforces — newer firmwares
        accept RequestControl from ENABLE as well):

          1. wait for first feedback frame
          2. wait out any INIT phase (~10s after PowerOn)
          3. flush motion queue if PAUSE / RUNNING / RECORDING
          4. clear ERROR if any
          5. RequestControl  — switches the controller into TCP mode
          6. if currently DISABLED:  PowerOn → EnableRobot → wait for ENABLE
             if currently ENABLE  :  no-op, already good

        Idempotent: safe to call repeatedly.
        """
        if self._dashboard is None or self._board is None:
            raise RuntimeError(f"call {self.name}.start() before acquire_control()")

        deadline = time.monotonic() + timeout_s

        # 1. wait for first feedback frame so robot_mode is populated
        while self._board.read(f"{self.name}.robot_mode") is None:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"{self.name}: no feedback received within {timeout_s}s"
                )
            time.sleep(poll_interval_s)

        # 2. wait out INIT phase
        while self._read_mode() == ROBOT_MODE_INIT:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"{self.name}: still in INIT after {timeout_s}s — "
                    "PowerOn() may not have been called or hardware is stuck"
                )
            time.sleep(poll_interval_s)

        # 3. flush any pending motion (PAUSE / RUNNING / RECORDING block handover)
        if self._read_mode() in ROBOT_MODES_NEED_STOP:
            logger.info(
                "%s: stopping motion queue (mode=%s) before handover",
                self.name, robot_mode_name(self._read_mode()),
            )
            self._dashboard.Stop()
            time.sleep(poll_interval_s)

        # 4. clear ERROR if any
        if self._read_mode() == ROBOT_MODE_ERROR:
            logger.info("%s: clearing controller error before handover", self.name)
            self._dashboard.ClearError()
            time.sleep(poll_interval_s)

        # 5. RequestControl — works from both DISABLED and ENABLE
        logger.info("%s: requesting TCP control (mode=%s)",
                    self.name, robot_mode_name(self._read_mode()))
        resp = self._dashboard.RequestControl()
        self._check_controller_response("RequestControl", resp)

        # 6. ensure ENABLE state
        mode = self._read_mode()
        if mode == ROBOT_MODE_ENABLE:
            logger.info("%s: already enabled, ready", self.name)
            return
        if mode != ROBOT_MODE_DISABLED:
            raise RuntimeError(
                f"{self.name}: unexpected mode {robot_mode_name(mode)} after RequestControl"
            )

        logger.info("%s: powering on", self.name)
        resp = self._dashboard.PowerOn()
        self._check_controller_response("PowerOn", resp)

        logger.info("%s: enabling robot", self.name)
        resp = self._dashboard.EnableRobot()
        self._check_controller_response("EnableRobot", resp)
        self._wait_until_mode(
            {ROBOT_MODE_ENABLE}, deadline,
            error_hint="EnableRobot did not transition to ENABLE",
        )
        logger.info("%s: ready (TCP control + enabled)", self.name)

    def release_control(self) -> None:
        """Drop the arm to DISABLED so the operator can take over from the pendant.

        Best-effort: errors are logged, not raised, because release_control is
        typically called from cleanup paths where raising would mask the real
        failure.
        """
        if self._dashboard is None:
            return
        try:
            # flush any pending motion before disabling — leaves the controller
            # in a clean state for the next acquire_control cycle
            self._dashboard.Stop()
        except Exception:  # noqa: BLE001
            logger.exception("%s: Stop failed during release_control", self.name)
        try:
            self._dashboard.DisableRobot()
        except Exception:  # noqa: BLE001
            logger.exception("%s: DisableRobot failed during release_control", self.name)

    def _read_mode(self) -> int:
        if self._board is None:
            return ROBOT_MODE_NO_CONTROLLER
        value = self._board.read(f"{self.name}.robot_mode")
        return ROBOT_MODE_NO_CONTROLLER if value is None else int(value)

    def _wait_until_mode(
        self,
        allowed: set[int],
        deadline: float,
        error_hint: str,
    ) -> None:
        while True:
            mode = self._read_mode()
            if mode in allowed:
                return
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"{self.name}: {error_hint} (currently {robot_mode_name(mode)})"
                )
            time.sleep(0.1)

    @staticmethod
    def _check_controller_response(cmd: str, resp: object) -> None:
        """Raise if the controller returned an error string instead of OK."""
        if not isinstance(resp, str):
            return
        if resp.startswith("0,") or resp.startswith("0 ,"):
            return  # ErrorID=0 means OK
        # The "Control Mode Is Not Tcp" we hit earlier comes through here as a
        # raw string with no ErrorID prefix. Treat anything that isn't "0,..."
        # as a controller-side rejection.
        raise RuntimeError(f"{cmd} rejected by controller: {resp!r}")

    def stop(self) -> None:
        # release control before shutting down — operator gets the arm back in DISABLED
        self.release_control()

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
        board.write(f"{prefix}.robot_mode", int(f0["RobotMode"]), writer=self.name)
