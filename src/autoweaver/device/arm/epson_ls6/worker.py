"""EpsonLS6Worker — push-side counterpart of the EpsonLS6 driver.

Owns one device's runtime context: holds the gRPC channel and the
``EpsonLS6`` driver, polls status on every tick, publishes business-
level state to the WorldBoard under ``<self.name>.*``, and exposes
async motion commands via the standard MotionWorker async-note
protocol.

BT leaves drive this Worker through ``NotifyAndWait``:

    NotifyAndWait(
        world_board=board,
        target="ls6_1",
        note_name="move_l",
        payload=lambda bb: {"target": (x, y, z, u), "speed": 30},
    )

The note handler issues the corresponding ``driver.move_*()`` call.
The MotionWorker base watches the busy / done / error_code edges on
``on_tick`` and writes ``<self.name>.last_completed_id`` when the
motion finishes — at which point ``NotifyAndWait`` flips to SUCCESS.

State fields published under namespace ``<self.name>``:

  - ``done`` (bool) — last motion completed (raw runtime flag)
  - ``busy`` (bool) — motion currently in progress
  - ``error_code`` (int) — 0 = clean; non-zero see SPEL+ ``ERR_*``
    constants in ``controller_program.spel``
  - ``pose`` (np.ndarray) — 4×4 flange pose matrix
  - ``joints`` (tuple) — joint angles (J1, J2, Z, J4) in deg / mm
  - ``last_request_id`` / ``last_completed_id`` / ``last_error`` —
    framework-managed (see MotionWorker)

Direct ``self.driver.move_l(...)`` calls still work for tests and
scripts but bypass the request_id protocol — leaves inside a BT should
always use the note path so sequential moves don't race on the
``done`` flag.
"""

from __future__ import annotations

import logging

import numpy as np

from autoweaver.device.arm.epson_ls6.driver import EpsonLS6, _scara_status_to_matrix
from autoweaver.motion_policy.runtime_client import RuntimeClient
from autoweaver.worker.base import TickContext
from autoweaver.worker.motion import MotionWorker

logger = logging.getLogger(__name__)


class EpsonLS6Worker(MotionWorker):
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
        self.accept_async_notes("jump", dict, self._dispatch_jump)

        # halt is synchronous from the BT's perspective. MotionWorker
        # doesn't provide a synchronous accept_notes, so wire it
        # directly through the board. We pop __request_id__ ourselves
        # because we're bypassing both wrappers; halt is always a
        # success at the protocol level so we just need to keep
        # last_completed_id moving.
        assert self._board is not None
        self._board.accept_notes(
            namespace=self._name,
            name="halt",
            payload_type=dict,
            on_receive=self._on_halt,
        )

    # ------------------------------------------------------------------
    # State publishing + completion detection (MotionWorker pattern)
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

        if status.error_code != 0:
            self.note_error(f"error_code={int(status.error_code)}")
        elif status.busy:
            self.note_busy_started()
        elif status.done:
            self.note_completion()

    # ------------------------------------------------------------------
    # Note handlers
    # ------------------------------------------------------------------

    def _dispatch_move_l(self, payload: dict) -> None:
        self.driver.move_l(
            tuple(payload["target"]),
            speed=payload.get("speed"),
            accel=payload.get("accel"),
        )

    def _dispatch_move_j(self, payload: dict) -> None:
        self.driver.move_j(
            tuple(payload["target"]),
            speed=payload.get("speed"),
            accel=payload.get("accel"),
        )

    def _dispatch_jump(self, payload: dict) -> None:
        self.driver.jump(
            tuple(payload["target"]),
            speed=payload.get("speed"),
            accel=payload.get("accel"),
        )

    def _on_halt(self, payload: dict) -> None:
        # halt is synchronous: cancel the pending motion (writes
        # last_completed_id so the BT doesn't hang) then call driver.
        # We still need to maintain last_request_id / last_completed_id
        # for the halt request itself, in case the caller used
        # NotifyAndWait — pop __request_id__ and complete it
        # immediately after the driver call.
        request_id = payload.pop("__request_id__", None)
        if request_id is not None:
            assert self._board is not None
            self._board.post_state(
                f"{self._name}.last_request_id", int(request_id),
                writer=self._name,
            )

        self.cancel_pending(reason="halt")

        try:
            self.driver.halt(0)  # NEXT-011: no-op in current impl
        except Exception:
            logger.exception("EpsonLS6Worker '%s': halt raised", self._name)

        if request_id is not None:
            assert self._board is not None
            self._board.post_state(
                f"{self._name}.last_completed_id", int(request_id),
                writer=self._name,
            )
