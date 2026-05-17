"""EpsonLS6Worker — push-side counterpart of the EpsonLS6 driver.

Owns one device's runtime context: holds the gRPC channel and the
``EpsonLS6`` driver, polls status on every tick, publishes business-
level state to the WorldBoard under ``<self.name>.*``, and exposes
async motion commands via the standard note + request_id protocol.

BT leaves drive this Worker through ``NotifyAndWait``:

    NotifyAndWait(
        world_board=board,
        target="ls6_1",
        note_name="move_l",
        payload=lambda bb: {"target": (x, y, z, u), "speed": 30},
    )

The note handler issues the corresponding ``driver.move_*()`` call and
records the pending request id. ``on_tick`` watches the SCARA status
``done`` flag and writes ``<self.name>.last_completed_id`` when the
motion finishes — at which point ``NotifyAndWait`` flips to SUCCESS.

State fields published under namespace ``<self.name>``:

  - ``done`` (bool) — last motion completed (raw runtime flag)
  - ``busy`` (bool) — motion currently in progress
  - ``error_code`` (int) — 0 = clean; non-zero see SPEL+ ``ERR_*``
    constants in ``controller_program.spel``
  - ``pose`` (np.ndarray) — 4×4 flange pose matrix
  - ``joints`` (tuple) — joint angles (J1, J2, Z, J4) in deg / mm
  - ``last_request_id`` / ``last_completed_id`` / ``last_error`` —
    framework-managed request protocol

Direct ``self.driver.move_l(...)`` calls still work for tests and
scripts but bypass the request_id protocol — leaves inside a BT should
always use the note path so sequential moves don't race on the
``done`` flag.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from autoweaver.device.arm.epson_ls6 import EpsonLS6, _scara_status_to_matrix
from autoweaver.motion_policy.runtime_client import RuntimeClient
from autoweaver.worker.base import TickContext, Worker

logger = logging.getLogger(__name__)


class EpsonLS6Worker(Worker):
    dof = 4

    def __init__(
        self,
        client: RuntimeClient,
        device_name: str,
        name: str,
        *,
        speed: int = 50,
        accel: int = 200,
    ):
        super().__init__()
        self._name = name
        self._client = client
        self._device_name = device_name
        self.driver = EpsonLS6(
            client, device_name, name, speed=speed, accel=accel,
        )

        # Pending move tracking. Only one move can be in-flight at a time;
        # accepting a new move_* note while one is pending logs a warning
        # and completes the older request to avoid leaving the BT hung.
        self._pending_move_rid: Optional[int] = None
        # ``_move_started`` flips True the first time we see busy=True
        # after the move was submitted; only then do we watch for the
        # busy → False transition that signals completion. Without this,
        # a stale "done=True" from before the submission would falsely
        # complete the request on the very first tick.
        self._move_started: bool = False

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

        # move_* are async motions — register raw so the framework's
        # auto-completion wrapper doesn't write last_completed_id at
        # handler return (which would be far too early). on_tick writes
        # it when the motion truly finishes.
        for note_name, handler in [
            ("move_l", self._on_move_l),
            ("move_j", self._on_move_j),
            ("jump", self._on_jump),
        ]:
            assert self._board is not None
            self._board.accept_notes(
                namespace=self._name,
                name=note_name,
                payload_type=dict,
                on_receive=handler,
            )

        # halt is fire-and-forget at the protocol level (currently no-op
        # until NEXT-011). The auto-wrapper's synchronous completion is
        # correct here.
        self.accept_notes("halt", dict, self._on_halt)

    # ------------------------------------------------------------------
    # State publishing + completion detection
    # ------------------------------------------------------------------

    def on_tick(self, ctx: TickContext) -> None:
        status = self._client.read_scara_status(self._device_name)

        self.write_state(f"{self._name}.done", status.done)
        self.write_state(f"{self._name}.busy", status.busy)
        self.write_state(f"{self._name}.error_code", int(status.error_code))
        self.write_state(f"{self._name}.pose", _scara_status_to_matrix(status))
        self.write_state(
            f"{self._name}.joints",
            (status.joint_1, status.joint_2, status.joint_3, status.joint_4),
        )

        if self._pending_move_rid is None:
            return

        # Error path: the controller raised an alarm. Surface it as
        # last_error + complete the pending request so the BT doesn't
        # hang waiting for a done that will never come.
        if status.error_code != 0:
            msg = (
                f"motion error during request rid={self._pending_move_rid}: "
                f"error_code={status.error_code}"
            )
            logger.error("EpsonLS6Worker '%s': %s", self._name, msg)
            self.write_state(f"{self._name}.last_error", msg)
            self._write_completion(self._pending_move_rid)
            self._pending_move_rid = None
            self._move_started = False
            return

        # Normal completion path: wait until we've observed busy=True
        # at least once (the move is actually in flight), then look for
        # busy=False with done=True (the move has finished).
        if not self._move_started:
            if status.busy:
                self._move_started = True
            return

        if not status.busy and status.done:
            self._write_completion(self._pending_move_rid)
            self._pending_move_rid = None
            self._move_started = False

    # ------------------------------------------------------------------
    # Note handlers
    # ------------------------------------------------------------------

    def _on_move_l(self, payload: dict) -> None:
        self._dispatch_motion(payload, self.driver.move_l)

    def _on_move_j(self, payload: dict) -> None:
        self._dispatch_motion(payload, self.driver.move_j)

    def _on_jump(self, payload: dict) -> None:
        self._dispatch_motion(payload, self.driver.jump)

    def _on_halt(self, payload: dict) -> None:
        # NEXT-011: halt against runtime is no-op for now (proto has no
        # goal_id). We still cancel local pending state so the BT
        # doesn't hang waiting for a request the user explicitly halted.
        if self._pending_move_rid is not None:
            self._write_completion(self._pending_move_rid)
        self._pending_move_rid = None
        self._move_started = False
        try:
            self.driver.halt(0)  # no-op in current impl
        except Exception:
            logger.exception("EpsonLS6Worker '%s': halt raised", self._name)

    def _dispatch_motion(self, payload: dict, motion_fn) -> None:
        rid = payload.pop("__request_id__", None)
        if rid is None:
            logger.warning(
                "EpsonLS6Worker '%s': motion note missing __request_id__ — "
                "BT must use NotifyAndWait to dispatch motion notes",
                self._name,
            )

        target = payload.get("target")
        if target is None:
            msg = "motion note payload missing 'target'"
            logger.error("EpsonLS6Worker '%s': %s", self._name, msg)
            self.write_state(f"{self._name}.last_error", msg)
            if rid is not None:
                self.write_state(f"{self._name}.last_request_id", int(rid))
                self._write_completion(int(rid))
            return

        # If a previous move is still pending, complete it so the
        # caller's NotifyAndWait doesn't hang — log loudly because this
        # usually means the BT dispatched two motions without waiting.
        if self._pending_move_rid is not None:
            logger.warning(
                "EpsonLS6Worker '%s': dispatched new motion while rid=%d "
                "still pending; force-completing the old rid",
                self._name, self._pending_move_rid,
            )
            self._write_completion(self._pending_move_rid)

        if rid is not None:
            self.write_state(f"{self._name}.last_request_id", int(rid))

        speed = payload.get("speed")
        accel = payload.get("accel")
        try:
            motion_fn(tuple(target), speed=speed, accel=accel)
        except Exception as exc:
            logger.exception(
                "EpsonLS6Worker '%s': motion dispatch failed (target=%s)",
                self._name, target,
            )
            self.write_state(f"{self._name}.last_error", repr(exc))
            if rid is not None:
                self._write_completion(int(rid))
            self._pending_move_rid = None
            self._move_started = False
            return

        self._pending_move_rid = int(rid) if rid is not None else None
        self._move_started = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_completion(self, rid: int) -> None:
        self.write_state(f"{self._name}.last_completed_id", int(rid))
