"""DobotWorker — push-side counterpart of the Dobot driver.

Owns one Dobot arm's TCP connection: holds the driver, polls the
feedback frame on every tick, publishes business-level state to the
WorldBoard under ``<self.name>.*``, and exposes async motion commands
via the standard note + request_id protocol.

BT leaves drive this Worker via ``NotifyAndWait``, exactly the same
way they drive ``EpsonLS6Worker``. The state field names match (done,
busy, error_code, pose, joints) so leaf code is dof- and brand-
agnostic — replacing a Dobot with an LS6 (where physically possible)
is a one-line wiring change, not a BT rewrite.

The async completion model differs from EpsonLS6 in implementation but
not in interface:

  - EpsonLS6 reads done/busy directly from the SCARA runtime status.
  - Dobot derives them from the controller ``RobotMode`` field
    (RUNNING = busy; anything else after RUNNING = done).

Both then write ``last_completed_id`` on the busy → done transition.

The "move to current pose" no-op case (controller skips motion because
target equals current) is handled by a tick-count grace period: if
busy never goes True within ``_NO_OP_TICK_THRESHOLD`` ticks of
dispatch, the request is treated as already complete. At BT 20Hz this
is ~1.5s, well beyond any legitimate motion that would have raised
busy by then.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from autoweaver.device.arm.dobot import Dobot
from autoweaver.device.arm.dobot_states import (
    ROBOT_MODE_ERROR,
    ROBOT_MODE_RUNNING,
)
from autoweaver.geometry import transforms
from autoweaver.worker.base import TickContext, Worker

logger = logging.getLogger(__name__)


# Convention matches Dobot SDK: ToolVectorActual is (x, y, z, rx, ry, rz)
# ZYX-intrinsic in degrees. See dobot.py for the rationale.
_POSE_RPY_CONVENTION = "zyx_intrinsic_deg"

# Ticks to wait for busy = True before treating a dispatched move as a
# no-op completion. At BT 20Hz this is ~1.5s.
_NO_OP_TICK_THRESHOLD = 30


class DobotWorker(Worker):
    dof = 6

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

        self._pending_move_rid: Optional[int] = None
        # ``_move_started`` flips True the first time we see busy=True
        # for this rid. Until then a stale done from before dispatch
        # would falsely complete the request.
        self._move_started: bool = False
        # No-op grace: if busy never goes True we assume the controller
        # skipped the motion (target == current pose).
        self._no_op_ticks: int = 0

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

        # Motion notes registered raw; on_tick manages last_completed_id.
        assert self._board is not None
        for note_name, handler in [
            ("move_l", self._on_move_l),
            ("move_j", self._on_move_j),
            ("move_j_joints", self._on_move_j_joints),
        ]:
            self._board.accept_notes(
                namespace=self._name,
                name=note_name,
                payload_type=dict,
                on_receive=handler,
            )

        # halt is synchronous from the BT's perspective; auto-wrapper OK.
        self.accept_notes("halt", dict, self._on_halt)

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
    # State publishing + completion detection
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

        if self._pending_move_rid is None:
            return

        # Error path: controller tripped (workspace limit, joint limit,
        # singularity, etc.). Surface as last_error and release the
        # pending rid so the BT doesn't hang.
        if error:
            rid = self._pending_move_rid
            msg = (
                f"controller raised alarm during motion rid={rid} "
                "(workspace / joint / singularity limit likely)"
            )
            logger.error("DobotWorker '%s': %s", self._name, msg)
            self.write_state(f"{self._name}.last_error", msg)
            self._write_completion(rid)
            self._reset_pending()
            return

        if not self._move_started:
            if busy:
                self._move_started = True
                self._no_op_ticks = 0
            else:
                # No-op grace period: target may equal current pose.
                self._no_op_ticks += 1
                if self._no_op_ticks >= _NO_OP_TICK_THRESHOLD:
                    logger.info(
                        "DobotWorker '%s': rid=%d never entered RUNNING "
                        "after %d ticks; treating as no-op completion",
                        self._name, self._pending_move_rid, self._no_op_ticks,
                    )
                    self._write_completion(self._pending_move_rid)
                    self._reset_pending()
            return

        # busy went True, now watch for True → False transition.
        if not busy:
            self._write_completion(self._pending_move_rid)
            self._reset_pending()

    # ------------------------------------------------------------------
    # Note handlers
    # ------------------------------------------------------------------

    def _on_move_l(self, payload: dict) -> None:
        self._dispatch_motion(payload, self.driver.move_l)

    def _on_move_j(self, payload: dict) -> None:
        self._dispatch_motion(payload, self.driver.move_j)

    def _on_move_j_joints(self, payload: dict) -> None:
        self._dispatch_motion(payload, self.driver.move_j_joints)

    def _on_halt(self, payload: dict) -> None:
        rid = self._pending_move_rid
        try:
            # Pass current pending goal id (or 0 if none — driver's halt
            # treats stale ids as no-ops).
            self.driver.halt(rid if rid is not None else 0)
        except Exception:
            logger.exception("DobotWorker '%s': halt raised", self._name)
        if rid is not None:
            self._write_completion(rid)
        self._reset_pending()

    def _dispatch_motion(self, payload: dict, motion_fn) -> None:
        rid = payload.pop("__request_id__", None)
        if rid is None:
            logger.warning(
                "DobotWorker '%s': motion note missing __request_id__ — "
                "BT must use NotifyAndWait to dispatch motion notes",
                self._name,
            )

        target = payload.get("target") or payload.get("pose")
        if target is None:
            msg = "motion note payload missing 'target' (or legacy 'pose') field"
            logger.error("DobotWorker '%s': %s", self._name, msg)
            self.write_state(f"{self._name}.last_error", msg)
            if rid is not None:
                self.write_state(f"{self._name}.last_request_id", int(rid))
                self._write_completion(int(rid))
            return

        if self._pending_move_rid is not None:
            logger.warning(
                "DobotWorker '%s': dispatched new motion while rid=%d "
                "still pending; force-completing the old rid",
                self._name, self._pending_move_rid,
            )
            self._write_completion(self._pending_move_rid)

        if rid is not None:
            self.write_state(f"{self._name}.last_request_id", int(rid))

        speed = int(payload.get("speed", self._default_speed))
        try:
            motion_fn(tuple(target), speed=speed)
        except Exception as exc:
            logger.exception(
                "DobotWorker '%s': motion dispatch failed (target=%s)",
                self._name, target,
            )
            self.write_state(f"{self._name}.last_error", repr(exc))
            if rid is not None:
                self._write_completion(int(rid))
            self._reset_pending()
            return

        self._pending_move_rid = int(rid) if rid is not None else None
        self._move_started = False
        self._no_op_ticks = 0

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

    def _write_completion(self, rid: int) -> None:
        self.write_state(f"{self._name}.last_completed_id", int(rid))

    def _reset_pending(self) -> None:
        self._pending_move_rid = None
        self._move_started = False
        self._no_op_ticks = 0
