from __future__ import annotations

import numpy as np
import pytest

from autoweaver.device.arm.base import ArmBase6
from autoweaver.device.arm.mock import MockArm


def _new_arm(**kwargs) -> MockArm:
    """Construct + start a MockArm. Most tests don't care about start()."""
    arm = MockArm(**kwargs)
    arm.start()
    return arm


def test_mock_arm_satisfies_arm_base_6_protocol():
    arm = MockArm(name="m1")
    assert isinstance(arm, ArmBase6)


def test_move_j_increments_goal_and_records_call():
    arm = _new_arm(name="m1")
    gid1 = arm.move_j((0, 0, 0, 0, 0, 0))
    gid2 = arm.move_j((1, 1, 1, 1, 1, 1))
    assert gid1 == 1
    assert gid2 == 2
    kinds = [c[0] for c in arm.calls]
    assert kinds == ["move_j", "move_j"]


def test_move_j_rejects_wrong_arity():
    arm = _new_arm(name="m1")
    with pytest.raises(ValueError):
        arm.move_j((1, 2, 3))


def test_halt_with_current_goal_clears_state():
    arm = _new_arm(name="m1")
    gid = arm.move_j((0, 0, 0, 0, 0, 0))
    arm.halt(gid)
    assert arm._current_goal_id is None
    assert ("halt", gid) in arm.calls


def test_halt_with_stale_goal_does_not_clear_current():
    arm = _new_arm(name="m1")
    gid1 = arm.move_j((0, 0, 0, 0, 0, 0))
    arm.halt(gid1)
    gid2 = arm.move_j((1, 1, 1, 1, 1, 1))
    arm.halt(gid1)  # stale
    assert arm._current_goal_id == gid2


def test_get_flange_pose_returns_4x4_matrix():
    arm = _new_arm(name="m1")
    m = arm.get_flange_pose()
    assert m.shape == (4, 4)
    assert np.allclose(m, np.eye(4))  # home pose is the identity


def test_get_flange_pose_reflects_set_pose():
    arm = _new_arm(name="m1")
    arm.set_pose((100, 200, 300, 0, 0, 0))
    m = arm.get_flange_pose()
    assert np.allclose(m[:3, 3], [100, 200, 300])
    assert np.allclose(m[:3, :3], np.eye(3))


def test_move_l_completes_immediately_when_duration_zero():
    arm = _new_arm(name="m1", move_duration=0.0)
    arm.move_l((1.0, 2.0, 3.0, 0.0, 0.0, 0.0))
    m = arm.get_flange_pose()
    assert np.allclose(m[:3, 3], [1.0, 2.0, 3.0])


def test_move_j_with_cartesian_target_updates_pose():
    """move_j is Cartesian-target PTP → simulated pose moves to target."""
    arm = _new_arm(name="m1", move_duration=0.0)
    arm.move_j((4.0, 5.0, 6.0, 0.0, 0.0, 0.0))
    m = arm.get_flange_pose()
    assert np.allclose(m[:3, 3], [4.0, 5.0, 6.0])


def test_move_j_joints_records_call_and_does_not_change_pose():
    """move_j_joints targets joints — the mock doesn't model FK, so
    Cartesian pose stays unchanged."""
    arm = _new_arm(name="m1", move_duration=0.0)
    arm.set_pose((100, 200, 300, 0, 0, 0))
    gid = arm.move_j_joints((10, 20, 30, 40, 50, 60))
    assert gid == 1
    assert ("move_j_joints", gid, (10.0, 20.0, 30.0, 40.0, 50.0, 60.0)) in arm.calls
    # Cartesian pose hasn't moved — mock doesn't synthesize FK.
    m = arm.get_flange_pose()
    assert np.allclose(m[:3, 3], [100, 200, 300])


def test_move_j_joints_rejects_wrong_arity():
    arm = _new_arm(name="m1")
    with pytest.raises(ValueError):
        arm.move_j_joints((1, 2, 3))


def test_move_j_joints_without_start_raises():
    arm = MockArm(name="m1")
    with pytest.raises(RuntimeError, match="start"):
        arm.move_j_joints((0, 0, 0, 0, 0, 0))


def test_halt_freezes_pose_at_current_value():
    arm = _new_arm(name="m1", move_duration=10.0)
    gid = arm.move_l((9.0, 9.0, 9.0, 0.0, 0.0, 0.0))
    # Pose hasn't reached target — move is still in flight.
    m_before = arm.get_flange_pose()
    arm.halt(gid)
    m_after = arm.get_flange_pose()
    # Halt cancelled the in-flight goal; pose should not have jumped to target.
    assert not np.allclose(m_after[:3, 3], [9.0, 9.0, 9.0])
    assert np.allclose(m_before, m_after)


def test_move_without_start_raises():
    arm = MockArm(name="m1")
    with pytest.raises(RuntimeError, match="start"):
        arm.move_j((0, 0, 0, 0, 0, 0))


def test_get_flange_pose_without_start_raises():
    arm = MockArm(name="m1")
    with pytest.raises(RuntimeError, match="start"):
        arm.get_flange_pose()


def test_stop_disables_subsequent_calls():
    arm = _new_arm(name="m1")
    arm.stop()
    with pytest.raises(RuntimeError, match="start"):
        arm.move_j((0, 0, 0, 0, 0, 0))


def test_dof_is_six():
    assert MockArm(name="m1").dof == 6
