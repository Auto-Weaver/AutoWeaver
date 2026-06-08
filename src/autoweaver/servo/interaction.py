"""Interaction matrix (image Jacobian) providers. See NEXT-013 §1.4, §2.

The interaction matrix ``L`` relates camera-frame / actuator velocity to
the time-derivative of the visual feature error: ``e_dot = L @ v``. IBVS
inverts this to drive the error to zero.

This module owns the **provider abstraction**, not a specific calibration.
Per NEXT-013 §2 seam ②, *where the matrix comes from* is deliberately left
pluggable:

  - ``ConstantInteractionMatrix`` — the first-version provider for the
    pluck cell. The telecentric lens gives orthographic projection (zoom
    independent of depth), so the depth term in the classic point-feature
    interaction matrix vanishes and ``L`` collapses to a constant 2x2 that
    maps "Epson commanded XY motion" → "tweezer-tip pixel displacement in
    the nova5 camera". This 2x2 is cross-arm and cannot be written in
    closed form (that would need the whole calibration chain we are trying
    to avoid) — so it is *measured / probed*, then frozen here.

  - A future ``BroydenInteractionMatrix`` (online, stateful, per NEXT-013
    §2 seam ②) will estimate ``L`` from observed (command, displacement)
    pairs without any calibration. It is not implemented yet; the protocol
    below is the contract it will satisfy.

The provider is intentionally a tiny protocol so the control law
(``law.ibvs_velocity``) depends only on "give me a matrix for the current
features", never on how that matrix was obtained.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class InteractionMatrix(Protocol):
    """Supplies the interaction matrix ``L`` for the current feature state.

    ``features`` is the current visual feature vector (e.g. the tweezer-tip
    pixel coordinates, or the tip→feather error in pixels) — providers that
    are configuration-dependent (the Broyden estimator) read it; the
    constant provider ignores it.

    Returns an ``(m, n)`` array where ``m`` = number of feature DOF being
    controlled and ``n`` = number of actuator DOF. For the pluck cell's
    XY-only case this is 2x2.
    """

    def matrix(self, features: np.ndarray) -> np.ndarray: ...


class ConstantInteractionMatrix:
    """A fixed interaction matrix — the telecentric, XY-only pluck case.

    The matrix is supplied once (probed offline, or hand-measured by
    jogging the Epson a known XY step and reading the tip's pixel
    displacement in the nova5 camera) and returned unchanged for every
    feature state. This is correct precisely when the projection is
    orthographic (telecentric lens) so the matrix has no depth dependence.

    Args:
        matrix: the ``(m, n)`` interaction matrix. Copied and frozen on
            construction so the provider is immutable.

    Raises:
        ValueError: matrix is not 2-D, or is empty.
    """

    def __init__(self, matrix: np.ndarray):
        arr = np.array(matrix, dtype=np.float64)
        if arr.ndim != 2:
            raise ValueError(
                f"interaction matrix must be 2-D (m, n), got shape {arr.shape}"
            )
        if arr.size == 0:
            raise ValueError("interaction matrix must be non-empty")
        arr.flags.writeable = False
        self._matrix = arr

    @property
    def shape(self) -> tuple[int, int]:
        return self._matrix.shape  # type: ignore[return-value]

    def matrix(self, features: np.ndarray) -> np.ndarray:
        # Constant: the feature state is irrelevant. Return the frozen
        # array directly — callers in law.ibvs_velocity treat it read-only.
        return self._matrix
