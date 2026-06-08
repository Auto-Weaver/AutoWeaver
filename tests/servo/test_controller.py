"""Tests for the servo iteration policy (ServoController)."""

from __future__ import annotations

import numpy as np
import pytest

from autoweaver.servo.controller import (
    ServoController,
    ServoOutcome,
)
from autoweaver.servo.interaction import ConstantInteractionMatrix


def _ctrl(**kw):
    """Controller with an identity 2x2 interaction matrix + test defaults."""
    defaults = dict(
        gain=1.0,
        deadband=1.0,
        max_step=None,
        max_iterations=20,
        divergence_ratio=3.0,
    )
    defaults.update(kw)
    return ServoController(ConstantInteractionMatrix(np.eye(2)), **defaults)


def test_step_emits_velocity_when_far_from_target():
    ctrl = _ctrl()
    d = ctrl.step(np.array([10.0, 0.0]), features=np.array([0.0, 0.0]))
    assert d.outcome is ServoOutcome.STEP
    assert d.is_terminal is False
    np.testing.assert_allclose(d.velocity, [-10.0, 0.0])
    assert d.iteration == 1


def test_converges_when_error_within_deadband():
    ctrl = _ctrl(deadband=2.0)
    d = ctrl.step(np.array([1.0, 1.0]), features=np.zeros(2))  # norm ~1.41 < 2
    assert d.outcome is ServoOutcome.CONVERGED
    assert d.succeeded is True
    assert d.is_terminal is True


def test_closed_loop_converges_over_iterations():
    """Drive a synthetic plant: error_next = error + L @ v. Identity L,
    gain 0.5 → contraction. Loop should report CONVERGED, not EXHAUSTED."""
    ctrl = _ctrl(gain=0.5, deadband=1.0, max_iterations=50)
    error = np.array([100.0, -50.0])
    outcome = None
    for _ in range(50):
        d = ctrl.step(error, features=np.zeros(2))
        if d.is_terminal:
            outcome = d.outcome
            break
        error = error + np.eye(2) @ d.velocity
    assert outcome is ServoOutcome.CONVERGED


def test_diverges_when_error_grows_past_guard():
    ctrl = _ctrl(divergence_ratio=3.0)
    # First step sets the initial error norm = 10.
    ctrl.step(np.array([10.0, 0.0]), features=np.zeros(2))
    # Now feed an error that's grown past 3x the initial (10 → 40 > 30).
    d = ctrl.step(np.array([40.0, 0.0]), features=np.zeros(2))
    assert d.outcome is ServoOutcome.DIVERGED
    assert d.is_terminal is True
    assert d.succeeded is False


def test_does_not_diverge_within_guard():
    ctrl = _ctrl(divergence_ratio=3.0)
    ctrl.step(np.array([10.0, 0.0]), features=np.zeros(2))  # initial = 10
    d = ctrl.step(np.array([25.0, 0.0]), features=np.zeros(2))  # 25 < 30
    assert d.outcome is ServoOutcome.STEP


def test_divergence_guard_can_be_disabled():
    ctrl = _ctrl(divergence_ratio=None)
    ctrl.step(np.array([10.0, 0.0]), features=np.zeros(2))
    d = ctrl.step(np.array([1000.0, 0.0]), features=np.zeros(2))
    assert d.outcome is ServoOutcome.STEP


def test_exhausts_after_max_iterations_without_converging():
    # Never converges (deadband tiny), no plant update → constant error.
    ctrl = _ctrl(deadband=0.1, max_iterations=3, divergence_ratio=None)
    outcomes = [
        ctrl.step(np.array([5.0, 0.0]), features=np.zeros(2)).outcome
        for _ in range(3)
    ]
    assert outcomes[0] is ServoOutcome.STEP
    assert outcomes[1] is ServoOutcome.STEP
    assert outcomes[2] is ServoOutcome.EXHAUSTED


def test_convergence_on_last_allowed_iteration_beats_exhaustion():
    """If the loop converges exactly on the max-th iteration, it reports
    CONVERGED — convergence is checked before the iteration cap."""
    ctrl = _ctrl(deadband=1.0, max_iterations=2, divergence_ratio=None)
    ctrl.step(np.array([5.0, 0.0]), features=np.zeros(2))  # iter 1: STEP
    d = ctrl.step(np.array([0.5, 0.0]), features=np.zeros(2))  # iter 2: within db
    assert d.outcome is ServoOutcome.CONVERGED


def test_max_step_clamps_emitted_velocity():
    ctrl = _ctrl(max_step=2.0)
    d = ctrl.step(np.array([30.0, 40.0]), features=np.zeros(2))  # raw norm 50
    assert d.clamped is True
    assert float(np.linalg.norm(d.velocity)) == pytest.approx(2.0)


def test_reset_clears_episode_state():
    ctrl = _ctrl(max_iterations=2, divergence_ratio=None, deadband=0.1)
    ctrl.step(np.array([5.0, 0.0]), features=np.zeros(2))
    ctrl.step(np.array([5.0, 0.0]), features=np.zeros(2))  # EXHAUSTED
    assert ctrl.iteration == 2
    ctrl.reset()
    assert ctrl.iteration == 0
    # After reset the initial-error baseline is re-established, so a large
    # error doesn't immediately count as divergence from the prior episode.
    d = ctrl.step(np.array([5.0, 0.0]), features=np.zeros(2))
    assert d.outcome is ServoOutcome.STEP
    assert d.iteration == 1


def test_uses_provider_matrix_for_the_step():
    """A non-identity provider matrix is actually used to compute v."""
    L = np.array([[0.0, 2.0], [-1.5, 0.0]])
    ctrl = ServoController(
        ConstantInteractionMatrix(L),
        gain=1.0, deadband=0.5, max_iterations=10, divergence_ratio=None,
    )
    error = np.array([6.0, 3.0])
    d = ctrl.step(error, features=np.zeros(2))
    # One exact step should null a synthetic plant: e + L @ v == 0.
    np.testing.assert_allclose(error + L @ d.velocity, [0.0, 0.0], atol=1e-9)


def test_rejects_bad_params():
    L = ConstantInteractionMatrix(np.eye(2))
    with pytest.raises(ValueError, match="gain must be positive"):
        ServoController(L, gain=0.0)
    with pytest.raises(ValueError, match="deadband must be >= 0"):
        ServoController(L, deadband=-1.0)
    with pytest.raises(ValueError, match="max_iterations must be positive"):
        ServoController(L, max_iterations=0)
    with pytest.raises(ValueError, match="divergence_ratio must be > 1"):
        ServoController(L, divergence_ratio=1.0)
    with pytest.raises(ValueError, match="max_step must be positive"):
        ServoController(L, max_step=-5.0)
