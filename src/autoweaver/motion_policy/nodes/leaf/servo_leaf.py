"""ServoLeaf — the BT leaf that closes the visual-servoing loop. NEXT-013.

This is the loop body's integration point: it ties the pure
``ServoController`` (servo/controller.py) to the WorldBoard and the arm
Worker, implementing the look-then-move cycle of NEXT-013 §4 Phase 1.

Per §1.6 ("A without B is meaningless"), the leaf does the *whole* loop —
read features → compute error → control law → command → wait → re-look —
not just half of it. It returns SUCCESS on convergence and FAILURE on
divergence / exhaustion, so it composes with the standard decorators
(``.retry()``, ``.timeout()``) and control nodes.

The freshness gate (§5)
-----------------------
Vision publishes at ~10 Hz; the clock ticks at ~50 Hz. Acting on every
tick would re-issue commands off the *same* stale frame and oscillate.
The leaf gates on ``frame_id``: it only takes a servo step when the frame
is newer than the one it last acted on, and holds (RUNNING, no command)
otherwise. This same gate gives look-then-move its "don't act on frames
captured during the move" property for free — after a move completes, the
leaf waits for a frame newer than the pre-move one before looking again.

The two soft seams (§2), injected as callables
----------------------------------------------
- **Seam ① — alignment intent.** ``error_fn(snapshot) -> (error,
  features)`` computes *which* feature aligns to *which* target (and any
  offset) in image space. That is policy/config, not control, so it lives
  outside the leaf. Returns the error vector the controller drives to zero
  and the feature vector the interaction-matrix provider may need.
- **Seam ③ — command transport.** ``command_fn(velocity, blackboard,
  snapshot) -> dict`` turns the controller's actuator-space velocity into
  the note payload for the arm Worker (e.g. current commanded XY + the
  velocity step → an absolute ``move_l`` target). Whether the arm takes
  relative or absolute targets is the arm's business, kept here behind
  this callable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import numpy as np

from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.servo.controller import ServoController, ServoOutcome
from autoweaver.worker.base import next_request_id

if TYPE_CHECKING:
    from autoweaver.motion_policy.blackboard import Blackboard
    from autoweaver.motion_policy.world_board import Snapshot, WorldBoard


# Seam ①: snapshot -> (error_vector, feature_vector), both 1-D arrays.
ErrorFn = Callable[["Snapshot"], "tuple[np.ndarray, np.ndarray]"]
# Seam ③: (velocity, blackboard, snapshot) -> note payload dict.
CommandFn = Callable[[np.ndarray, "Blackboard", "Snapshot"], dict]


class _Phase:
    """Internal sub-state of the look-then-move cycle."""

    LOOK = "look"      # waiting for a fresh frame to compute error + step
    MOVING = "moving"  # a step command is in flight; await completion


class ServoLeaf(TreeNode):
    """Run an IBVS look-then-move loop until convergence.

    Returns:
        - RUNNING while iterating (holding for a fresh frame, or waiting
          for a dispatched move to complete).
        - SUCCESS when the controller reports CONVERGED.
        - FAILURE when the controller reports DIVERGED or EXHAUSTED.

    Re-runnable: ``reset`` clears both the leaf's cycle state and the
    controller's episode state, so the leaf works inside ``Repeat`` (one
    feather after another) and under ``Retry`` (re-attempt after abort).

    Args:
        world_board: the board to dispatch step commands through.
        controller: the IBVS iteration policy (carries gain, deadband,
            clamp, iteration cap, divergence guard, interaction matrix).
        target: the arm Worker's name (note namespace) to command.
        note_name: the note the arm Worker accepts for a servo step
            (e.g. ``"move_l"``).
        error_fn: seam ① — computes ``(error, features)`` from the
            snapshot.
        command_fn: seam ③ — builds the note payload from the controller's
            velocity.
        frame_id_key: state key carrying the perception frame counter, for
            the freshness gate (e.g. ``"vision.frame_id"``).
        sender: note sender label (defaults to the leaf name).
        name: BT node name.
    """

    def __init__(
        self,
        world_board: "WorldBoard",
        controller: ServoController,
        *,
        target: str,
        note_name: str,
        error_fn: ErrorFn,
        command_fn: CommandFn,
        frame_id_key: str,
        sender: str | None = None,
        name: str = "",
    ):
        super().__init__(name=name or f"ServoLeaf({target}.{note_name})")
        self._wb = world_board
        self._controller = controller
        self._target = target
        self._note_name = note_name
        self._error_fn = error_fn
        self._command_fn = command_fn
        self._frame_id_key = frame_id_key
        self._sender = sender or self.name

        self._phase = _Phase.LOOK
        self._last_frame_id: int | None = None
        self._rid = 0

    def on_start(self) -> Status:
        # Fresh episode: clear controller + cycle state. (reset() also does
        # this, but on_start may run without a prior reset on first use.)
        self._controller.reset()
        self._phase = _Phase.LOOK
        self._last_frame_id = None
        self._rid = 0
        return self._evaluate()

    def on_running(self) -> Status:
        return self._evaluate()

    def reset(self) -> None:
        self._controller.reset()
        self._phase = _Phase.LOOK
        self._last_frame_id = None
        self._rid = 0
        super().reset()

    def _evaluate(self) -> Status:
        if self._phase == _Phase.MOVING:
            return self._await_move()
        return self._look()

    def _look(self) -> Status:
        # Freshness gate: act only on a frame newer than the last one we
        # acted on. No fresh frame → hold (no command), stay RUNNING.
        frame_id = self.snapshot.get(self._frame_id_key)
        if frame_id is None or frame_id == self._last_frame_id:
            return Status.RUNNING
        self._last_frame_id = frame_id

        error, features = self._error_fn(self.snapshot)
        decision = self._controller.step(error, features)

        if decision.outcome is ServoOutcome.CONVERGED:
            return Status.SUCCESS
        if decision.outcome in (ServoOutcome.DIVERGED, ServoOutcome.EXHAUSTED):
            return Status.FAILURE

        # STEP: dispatch the velocity as a move and wait for completion.
        payload = dict(self._command_fn(decision.velocity, self._blackboard, self.snapshot))
        self._rid = next_request_id()
        payload["__request_id__"] = self._rid
        self._wb.pass_note(
            namespace=self._target,
            name=self._note_name,
            payload=payload,
            sender=self._sender,
        )
        self._phase = _Phase.MOVING
        return Status.RUNNING

    def _await_move(self) -> Status:
        last_completed = self.snapshot.get(f"{self._target}.last_completed_id")
        if last_completed is None:
            last_completed = 0
        if int(last_completed) >= self._rid:
            # Move done. Back to LOOK; the freshness gate now holds until a
            # frame newer than the pre-move one arrives (don't act on a
            # frame captured mid-move). This is Phase-1 "don't look while
            # moving" with zero extra machinery.
            self._phase = _Phase.LOOK
            return Status.RUNNING
        return Status.RUNNING
