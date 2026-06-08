"""Servo policy engine — the iteration logic around the IBVS step.

Pure, hardware-free, board-free. Wraps ``law.ibvs_velocity`` with the
across-iterations bookkeeping that turns single steps into a converging
(or aborting) loop: convergence deadband, divergence detection, and an
iteration cap. This is the reusable *safety envelope* NEXT-013 §5 asks
for, extracted from any BT / WorldBoard concern so it can be tested on
synthetic error sequences.

What it owns
------------
- Per-iteration decision: emit a clamped velocity command, or declare the
  loop CONVERGED / DIVERGED / EXHAUSTED.
- The interaction-matrix provider (``InteractionMatrix``), so each step
  uses the right Jacobian for the current features.

What it deliberately does NOT own (the soft seams)
--------------------------------------------------
- **Seam ①: alignment intent.** It takes the feature *error* as an input,
  not the raw features — *which* point aligns to *which* target (and any
  offset) is computed by the caller and is config, not control.
- **Seam ③: command transport / freshness.** It does not read the
  WorldBoard, allocate request ids, or know about frame_id staleness. The
  ``ServoLeaf`` drives it: one ``step()`` call per fresh observation, and
  the leaf dispatches the returned velocity to the arm.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from autoweaver.servo.interaction import InteractionMatrix
from autoweaver.servo.law import ibvs_velocity


class ServoOutcome(Enum):
    """The classification of one servo iteration."""

    STEP = "step"            # emit the velocity, keep going
    CONVERGED = "converged"  # error within deadband — done, success
    DIVERGED = "diverged"    # error grew past the divergence guard — abort
    EXHAUSTED = "exhausted"  # hit max_iterations without converging — abort


@dataclass(frozen=True)
class ServoDecision:
    """Result of one ``ServoController.step``.

    Attributes:
        outcome: which of the four cases this iteration is.
        velocity: the actuator command (n-vector) — only meaningful when
            ``outcome is STEP``; a zero vector otherwise.
        error_norm: the L2 norm of the error this iteration saw (px).
        iteration: 1-based index of this iteration.
        clamped: whether ``max_step`` bound the velocity this iteration.
    """

    outcome: ServoOutcome
    velocity: np.ndarray
    error_norm: float
    iteration: int
    clamped: bool

    @property
    def is_terminal(self) -> bool:
        return self.outcome is not ServoOutcome.STEP

    @property
    def succeeded(self) -> bool:
        return self.outcome is ServoOutcome.CONVERGED


class ServoController:
    """Stateful IBVS iteration policy. One instance per servo episode.

    Construct with the loop's safety envelope, then call ``step(error,
    features)`` once per fresh observation. The controller tracks the
    iteration count and the initial error magnitude (for the divergence
    guard) and decides each call whether to command a move or terminate.

    Args:
        interaction_matrix: provider for ``L`` (e.g. ``ConstantInteraction
            Matrix`` for the telecentric XY pluck case).
        gain: IBVS proportional gain ``lambda`` (> 0).
        deadband: convergence threshold on ``||error||`` (px). At or below
            this, the loop is CONVERGED. Must be >= 0.
        max_step: per-iteration command magnitude cap (actuator units, e.g.
            mm), or ``None`` for no clamp. The look-then-move safety bound.
        max_iterations: hard cap on iterations before EXHAUSTED (> 0).
        divergence_ratio: if ``||error||`` exceeds this multiple of the
            initial error, the loop is DIVERGED. ``None`` disables the
            guard. Must be > 1 when set (the error must *grow* past the
            start to count as diverging).

    Raises:
        ValueError: any parameter out of range.
    """

    def __init__(
        self,
        interaction_matrix: InteractionMatrix,
        *,
        gain: float = 0.5,
        deadband: float = 2.0,
        max_step: float | None = None,
        max_iterations: int = 20,
        divergence_ratio: float | None = 3.0,
    ):
        if gain <= 0.0:
            raise ValueError(f"gain must be positive, got {gain}")
        if deadband < 0.0:
            raise ValueError(f"deadband must be >= 0, got {deadband}")
        if max_step is not None and max_step <= 0.0:
            raise ValueError(f"max_step must be positive, got {max_step}")
        if max_iterations <= 0:
            raise ValueError(
                f"max_iterations must be positive, got {max_iterations}"
            )
        if divergence_ratio is not None and divergence_ratio <= 1.0:
            raise ValueError(
                f"divergence_ratio must be > 1, got {divergence_ratio}"
            )

        self._L = interaction_matrix
        self._gain = gain
        self._deadband = deadband
        self._max_step = max_step
        self._max_iterations = max_iterations
        self._divergence_ratio = divergence_ratio

        self._iteration = 0
        self._initial_error_norm: float | None = None

    @property
    def iteration(self) -> int:
        """Number of ``step`` calls made so far this episode."""
        return self._iteration

    def reset(self) -> None:
        """Clear episode state so the controller can run a fresh loop.

        Called by ``ServoLeaf.reset`` so the leaf is re-runnable (e.g.
        inside a Repeat over multiple feathers, or on Retry after an
        aborted attempt)."""
        self._iteration = 0
        self._initial_error_norm = None

    def step(self, error: np.ndarray, features: np.ndarray) -> ServoDecision:
        """Advance the loop one iteration.

        Order of checks matters: convergence is tested *before* the
        iteration cap so a loop that converges exactly on its last allowed
        iteration reports CONVERGED, not EXHAUSTED. Divergence is tested
        after convergence (a converged loop never diverges) but before
        emitting a command.

        Args:
            error: the feature error ``e`` (m-vector, px), computed by the
                caller per seam ① (alignment intent).
            features: the current feature vector, forwarded to the
                interaction-matrix provider (ignored by the constant one).

        Returns:
            A ``ServoDecision``. When terminal (CONVERGED / DIVERGED /
            EXHAUSTED) the velocity is a zero vector.
        """
        e = np.asarray(error, dtype=np.float64).reshape(-1)
        error_norm = float(np.linalg.norm(e))
        self._iteration += 1

        if self._initial_error_norm is None:
            self._initial_error_norm = error_norm

        # 1. Converged? (checked first — see docstring.)
        if error_norm <= self._deadband:
            return self._terminal(ServoOutcome.CONVERGED, error_norm)

        # 2. Diverged? Error grew past the guard relative to the start.
        if (
            self._divergence_ratio is not None
            and self._initial_error_norm > 0.0
            and error_norm > self._divergence_ratio * self._initial_error_norm
        ):
            return self._terminal(ServoOutcome.DIVERGED, error_norm)

        # 3. Out of iterations?
        if self._iteration >= self._max_iterations:
            return self._terminal(ServoOutcome.EXHAUSTED, error_norm)

        # 4. Otherwise, emit a control step.
        servo_step = ibvs_velocity(
            e,
            self._L.matrix(np.asarray(features, dtype=np.float64)),
            gain=self._gain,
            max_step=self._max_step,
        )
        return ServoDecision(
            outcome=ServoOutcome.STEP,
            velocity=servo_step.velocity,
            error_norm=error_norm,
            iteration=self._iteration,
            clamped=servo_step.clamped,
        )

    def _terminal(self, outcome: ServoOutcome, error_norm: float) -> ServoDecision:
        return ServoDecision(
            outcome=outcome,
            velocity=np.zeros(0),
            error_norm=error_norm,
            iteration=self._iteration,
            clamped=False,
        )
