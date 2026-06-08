"""Tests for the IBVS control law — ``v = -lambda * J^+ * e``."""

from __future__ import annotations

import numpy as np
import pytest

from autoweaver.servo.law import ibvs_velocity


def test_identity_matrix_steps_directly_down_the_error():
    # L = I, gain = 1 → v = -e. The step is the negated error.
    error = np.array([3.0, -4.0])
    step = ibvs_velocity(error, np.eye(2), gain=1.0)
    np.testing.assert_allclose(step.velocity, [-3.0, 4.0])
    assert step.error_norm == pytest.approx(5.0)
    assert step.clamped is False


def test_gain_scales_the_step():
    error = np.array([2.0, 0.0])
    step = ibvs_velocity(error, np.eye(2), gain=0.5)
    np.testing.assert_allclose(step.velocity, [-1.0, 0.0])


def test_cross_arm_2x2_inverts_the_mapping():
    """A non-identity 2x2 (Epson XY → camera px) is inverted by L^+.

    With L mapping actuator → pixel, one step of v = -L^+ e applied to the
    plant (e_next ≈ e + L v) should null the error when L is exact.
    """
    L = np.array([[0.0, 2.0], [-1.5, 0.0]])  # rotated + scaled cross-arm map
    error = np.array([6.0, 3.0])
    step = ibvs_velocity(error, L, gain=1.0)
    # Plant update: new pixel error = e + L @ v. Exact L + gain 1 → zero.
    residual = error + L @ step.velocity
    np.testing.assert_allclose(residual, [0.0, 0.0], atol=1e-9)


def test_zero_error_gives_zero_velocity():
    step = ibvs_velocity(np.zeros(2), np.eye(2), gain=1.0)
    np.testing.assert_allclose(step.velocity, [0.0, 0.0])
    assert step.error_norm == 0.0


def test_max_step_clamps_magnitude_and_preserves_direction():
    error = np.array([30.0, 40.0])  # norm 50
    step = ibvs_velocity(error, np.eye(2), gain=1.0, max_step=5.0)
    # raw velocity would be [-30, -40] (norm 50); clamp to norm 5.
    assert step.clamped is True
    assert float(np.linalg.norm(step.velocity)) == pytest.approx(5.0)
    # direction preserved: parallel to [-30, -40] i.e. [-3, -4].
    np.testing.assert_allclose(step.velocity, [-3.0, -4.0])


def test_max_step_does_not_fire_below_threshold():
    error = np.array([3.0, 0.0])
    step = ibvs_velocity(error, np.eye(2), gain=1.0, max_step=10.0)
    assert step.clamped is False
    np.testing.assert_allclose(step.velocity, [-3.0, 0.0])


def test_iterating_converges_under_exact_model():
    """Closed-loop sanity: repeated steps drive a real plant error to 0.

    Model the plant as pixel_error_next = pixel_error + L @ v with a gain
    < 1 so it's a contraction, not a one-shot deadbeat. Error norm must
    decrease monotonically to ~0.
    """
    L = np.array([[1.2, 0.3], [-0.4, 0.9]])
    error = np.array([100.0, -80.0])
    norms = [float(np.linalg.norm(error))]
    for _ in range(50):
        step = ibvs_velocity(error, L, gain=0.3)
        error = error + L @ step.velocity
        norms.append(float(np.linalg.norm(error)))
    # Monotone non-increasing and converged near zero.
    assert all(b <= a + 1e-9 for a, b in zip(norms, norms[1:]))
    assert norms[-1] < 1e-3


def test_non_square_uses_least_squares():
    """3 feature DOF, 2 actuator DOF → overdetermined; pinv least-squares."""
    L = np.array([[1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])
    error = np.array([2.0, 3.0, 99.0])  # 3rd component unreachable
    step = ibvs_velocity(error, L, gain=1.0)
    # Only the first two DOF are controllable; step nulls them, ignores 3rd.
    assert step.velocity.shape == (2,)
    np.testing.assert_allclose(step.velocity, [-2.0, -3.0])


def test_rejects_non_positive_gain():
    with pytest.raises(ValueError, match="gain must be positive"):
        ibvs_velocity(np.array([1.0]), np.eye(1), gain=0.0)


def test_rejects_non_positive_max_step():
    with pytest.raises(ValueError, match="max_step must be positive"):
        ibvs_velocity(np.array([1.0]), np.eye(1), gain=1.0, max_step=0.0)


def test_rejects_shape_mismatch():
    with pytest.raises(ValueError, match="feature DOF must match"):
        ibvs_velocity(np.array([1.0, 2.0, 3.0]), np.eye(2), gain=1.0)


def test_rejects_non_2d_matrix():
    with pytest.raises(ValueError, match="must be 2-D"):
        ibvs_velocity(np.array([1.0]), np.array([1.0, 2.0]), gain=1.0)
