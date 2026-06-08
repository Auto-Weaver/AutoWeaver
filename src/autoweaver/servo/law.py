"""IBVS control law — ``v = -lambda * J^+ * e``. See NEXT-013 §1.2, §2.

The loop body's arithmetic core: given a visual feature error and an
interaction matrix, compute the actuator velocity / step that drives the
error toward zero. Pure function over numpy — no hardware, no WorldBoard,
no time. This is the part NEXT-013 §2 marks "low risk, build + test now
on synthetic data".

Design notes
------------
- **Pseudo-inverse, not inverse.** The interaction matrix may be
  non-square (more feature DOF than actuator DOF, or vice versa). The
  Moore-Penrose pseudo-inverse ``J^+`` gives the least-squares step that
  is correct in both the over- and under-determined cases, and reduces to
  the ordinary inverse for the square full-rank pluck-cell 2x2.
- **Gain ``lambda``.** A scalar proportional gain. The classic IBVS form;
  an exponential decoupled decrease of the error when ``L`` is constant
  and accurate. Kept simple — adaptive gain is a later refinement.
- **Step clamping.** ``max_step`` bounds the per-iteration command
  magnitude (in actuator units, e.g. mm). This is the primary safety
  envelope for look-then-move: a wild error (perception glitch, wrong
  feature) cannot command a large lunge. Clamping preserves direction.
- **No state.** Convergence / divergence are *classified* from the error
  magnitude by the caller across iterations; this function is one step.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ServoStep:
    """One IBVS iteration's output.

    Attributes:
        velocity: the actuator-space command for this step (n-vector, e.g.
            (dx, dy) in mm). Already gain-scaled and clamped.
        error_norm: the L2 norm of the input feature error (px). The scalar
            the caller thresholds for convergence / divergence.
        clamped: whether ``max_step`` bound the velocity this step.
    """

    velocity: np.ndarray
    error_norm: float
    clamped: bool


def ibvs_velocity(
    error: np.ndarray,
    interaction_matrix: np.ndarray,
    *,
    gain: float = 1.0,
    max_step: float | None = None,
) -> ServoStep:
    """One IBVS control step: ``v = -gain * L^+ * e``, then clamp.

    Args:
        error: feature error ``e`` (m-vector, px). For the pluck cell this
            is ``tip_px - feather_px`` (the tip-to-target offset in image
            space). Driving it to zero aligns the tweezer tip with the
            feather.
        interaction_matrix: ``L`` (m x n), from an ``InteractionMatrix``
            provider. ``L^+`` maps feature error → actuator step.
        gain: proportional gain ``lambda`` (> 0). Larger = faster but less
            stable once dead time enters the loop (NEXT-013 §4 Phase 2).
        max_step: optional cap on ``||v||`` in actuator units. If the raw
            step exceeds it, the step is rescaled to this magnitude
            (direction preserved). ``None`` = no clamp.

    Returns:
        A ``ServoStep`` with the (clamped) velocity, the input error norm,
        and whether clamping fired.

    Raises:
        ValueError: gain is not positive, max_step is not positive, or the
            error / matrix shapes are inconsistent.
    """
    if gain <= 0.0:
        raise ValueError(f"gain must be positive, got {gain}")
    if max_step is not None and max_step <= 0.0:
        raise ValueError(f"max_step must be positive, got {max_step}")

    e = np.asarray(error, dtype=np.float64).reshape(-1)
    L = np.asarray(interaction_matrix, dtype=np.float64)
    if L.ndim != 2:
        raise ValueError(f"interaction matrix must be 2-D, got shape {L.shape}")
    if L.shape[0] != e.shape[0]:
        raise ValueError(
            f"error has {e.shape[0]} elements but interaction matrix has "
            f"{L.shape[0]} rows; feature DOF must match"
        )

    error_norm = float(np.linalg.norm(e))

    # v = -lambda * L^+ * e. pinv handles non-square / rank-deficient L
    # and reduces to the plain inverse for the square full-rank 2x2.
    L_pinv = np.linalg.pinv(L)
    velocity = -gain * (L_pinv @ e)

    clamped = False
    if max_step is not None:
        step_norm = float(np.linalg.norm(velocity))
        if step_norm > max_step:
            # Rescale to max_step, preserving direction. step_norm > 0 here
            # because max_step > 0 and step_norm > max_step.
            velocity = velocity * (max_step / step_norm)
            clamped = True

    return ServoStep(velocity=velocity, error_norm=error_norm, clamped=clamped)
