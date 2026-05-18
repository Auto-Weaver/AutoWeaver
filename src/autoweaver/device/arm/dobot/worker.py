"""DobotWorker — push-side counterpart of the Dobot driver.

Owns one Dobot arm's TCP connection: holds the driver, polls the
feedback frame on every tick, publishes business-level state to the
WorldBoard under ``<self.name>.*``, and exposes async motion commands
via the standard MotionWorker async-note protocol.

BT leaves drive this Worker via ``NotifyAndWait``, exactly the same
way they drive ``EpsonLS6Worker``. The state field names match (done,
busy, error_code, pose, joints) so leaf code is dof- and brand-
agnostic — replacing a Dobot with an LS6 (where physically possible)
is a one-line wiring change, not a BT rewrite.

The async completion model differs from EpsonLS6 in *what* is read
each tick, not in *how* completion is signalled:

  - EpsonLS6 reads done/busy directly from the SCARA runtime status.
  - Dobot derives them from the controller ``RobotMode`` field
    (RUNNING = busy; anything else after RUNNING = done).

Both then feed busy/done/error to the same MotionWorker edge helpers.

The "move to current pose" no-op case (controller skips motion because
target equals current) is handled by MotionWorker's no_op_tick
mechanism. ``no_op_tick_threshold = 30`` ≈ 1.5s at BT 20Hz, well
beyond any legitimate motion that would have raised busy by then.
"""

from __future__ import annotations

import logging

import numpy as np

from autoweaver.device.arm.dobot.driver import Dobot
from autoweaver.device.arm.dobot.states import (
    ROBOT_MODE_ERROR,
    ROBOT_MODE_RUNNING,
)
from autoweaver.geometry import transforms
from autoweaver.worker.base import TickContext
from autoweaver.worker.motion import MotionWorker

logger = logging.getLogger(__name__)


# Convention matches Dobot SDK: ToolVectorActual is (x, y, z, rx, ry, rz)
# ZYX-intrinsic in degrees. See driver.py for the rationale.
_POSE_RPY_CONVENTION = "zyx_intrinsic_deg"


class DobotWorker(MotionWorker):
    dof = 6

    # Enable MotionWorker's no-op grace: if busy never goes True
    # within this many ticks of dispatch, treat the request as a
    # controller-skipped no-op (target == current pose).
    no_op_tick_threshold = 30

    def __init__(
        self,
        ip: str,
        name: str,
        *,
        default_speed: int = 30,
    ):
        super().__init__()
        self._name = name
        self.driver = Dobot(ip=ip, name=name)
        self._default_speed = default_speed

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_attach(self) -> None:
        self.declare_state(f"{self._name}.done", bool)
        self.declare_state(f"{self._name}.busy", bool)
        self.declare_state(f"{self._name}.error_code", int)
        self.declare_state(f"{self._name}.pose", np.ndarray)
        self.declare_state(f"{self._name}.joints", tuple)

        self.accept_async_notes("move_l", dict, self._dispatch_move_l)
        self.accept_async_notes("move_j", dict, self._dispatch_move_j)
        self.accept_async_notes(
            "move_j_joints", dict, self._dispatch_move_j_joints,
        )

        # halt is synchronous from the BT's perspective. See
        # EpsonLS6Worker for why this bypasses MotionWorker's wrapper.
        assert self._board is not None
        self._board.accept_notes(
            namespace=self._name,
            name="halt",
            payload_type=dict,
            on_receive=self._on_halt,
        )

    def on_start(self) -> None:
        self.driver.start()
        try:
            self.driver.acquire_control()
        except Exception:
            self.driver.stop()
            raise

    def on_stop(self) -> None:
        try:
            self.driver.stop()
        except Exception:
            logger.exception("DobotWorker '%s': driver.stop raised", self._name)

    # ------------------------------------------------------------------
    # State publishing + completion detection (MotionWorker pattern)
    # ------------------------------------------------------------------

    def on_tick(self, ctx: TickContext) -> None:
        frame = self._safe_pull_frame()
        if frame is None:
            return

        robot_mode = int(frame["RobotMode"])
        busy = robot_mode == ROBOT_MODE_RUNNING
        error = robot_mode == ROBOT_MODE_ERROR
        done = not busy and not error

        pose_vec = frame["ToolVectorActual"]
        pose_matrix = transforms.euler_to_matrix(
            np.asarray(pose_vec[:3], dtype=np.float64),
            pose_vec[3:],
            _POSE_RPY_CONVENTION,
        )
        joints = tuple(float(q) for q in frame["QActual"])

        self.write_state(f"{self._name}.done", done)
        self.write_state(f"{self._name}.busy", busy)
        # error_code: 0 = clean; non-zero = controller in ERROR mode.
        # Finer breakdown (which specific alarm) lives in
        # ErrorStatus / GetErrorID() — surface those if there's a real
        # downstream need; for now BT only needs "is the arm tripped".
        self.write_state(f"{self._name}.error_code", robot_mode if error else 0)
        self.write_state(f"{self._name}.pose", pose_matrix)
        self.write_state(f"{self._name}.joints", joints)

        if error:
            self.note_error(
                f"controller alarm (RobotMode={robot_mode}: workspace / "
                "joint / singularity limit likely)"
            )
        elif busy:
            self.note_busy_started()
        elif done:
            # If busy was ever seen, complete; otherwise count idle
            # ticks toward no-op grace.
            self.note_completion()
            self.note_idle_tick()

    # ------------------------------------------------------------------
    # Note handlers
    # ------------------------------------------------------------------

    def _dispatch_move_l(self, payload: dict) -> None:
        target = self._extract_target(payload)
        speed = int(payload.get("speed", self._default_speed))
        self.driver.move_l(tuple(target), speed=speed)

    def _dispatch_move_j(self, payload: dict) -> None:
        target = self._extract_target(payload)
        speed = int(payload.get("speed", self._default_speed))
        self.driver.move_j(tuple(target), speed=speed)

    def _dispatch_move_j_joints(self, payload: dict) -> None:
        target = self._extract_target(payload)
        speed = int(payload.get("speed", self._default_speed))
        self.driver.move_j_joints(tuple(target), speed=speed)

    @staticmethod
    def _extract_target(payload: dict):
        """Pull the motion target from a payload.

        Accepts ``target`` (canonical) and ``pose`` (legacy alias).
        Raises KeyError with a clear name so the framework's dispatch
        wrapper records a helpful ``last_error``.
        """
        target = payload.get("target")
        if target is None:
            target = payload.get("pose")
        if target is None:
            raise KeyError(
                "motion note payload missing 'target' (or legacy 'pose')"
            )
        return target

    def _on_halt(self, payload: dict) -> None:
        request_id = payload.pop("__request_id__", None)
        if request_id is not None:
            assert self._board is not None
            self._board.post_state(
                f"{self._name}.last_request_id", int(request_id),
                writer=self._name,
            )

        # Use the current pending request id if there is one; the
        # driver's halt treats stale ids as no-ops.
        halt_target = self._pending_request_id if self._pending_request_id is not None else 0
        self.cancel_pending(reason="halt")

        try:
            self.driver.halt(halt_target)
        except Exception:
            logger.exception("DobotWorker '%s': halt raised", self._name)

        if request_id is not None:
            assert self._board is not None
            self._board.post_state(
                f"{self._name}.last_completed_id", int(request_id),
                writer=self._name,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _safe_pull_frame(self):
        """Pull a feedback frame, returning None if unavailable.

        The Dobot SDK raises RuntimeError if no frame has arrived yet —
        treat that as "skip this tick" rather than faulting the Worker.
        """
        try:
            return self.driver._pull_frame()
        except RuntimeError:
            return None
