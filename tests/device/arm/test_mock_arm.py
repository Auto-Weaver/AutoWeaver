from __future__ import annotations

import time

import pytest

from autoweaver.device.arm.base import ArmBase
from autoweaver.device.arm.mock import MockArm
from autoweaver.motion_policy.world_board import WorldBoard


def test_mock_arm_satisfies_arm_base_protocol():
    arm = MockArm(name="m1")
    assert isinstance(arm, ArmBase)


def test_move_j_increments_goal_and_records_call():
    arm = MockArm(name="m1")
    gid1 = arm.move_j((0, 0, 0, 0, 0, 0))
    gid2 = arm.move_j((1, 1, 1, 1, 1, 1))
    assert gid1 == 1
    assert gid2 == 2
    kinds = [c[0] for c in arm.calls]
    assert kinds == ["move_j", "move_j"]


def test_move_j_rejects_wrong_arity():
    arm = MockArm(name="m1")
    with pytest.raises(ValueError):
        arm.move_j((1, 2, 3))


def test_halt_with_current_goal_clears_state():
    arm = MockArm(name="m1")
    gid = arm.move_j((0, 0, 0, 0, 0, 0))
    arm.halt(gid)
    assert arm._current_goal_id is None
    assert ("halt", gid) in arm.calls


def test_halt_with_stale_goal_does_not_clear_current():
    arm = MockArm(name="m1")
    gid1 = arm.move_j((0, 0, 0, 0, 0, 0))
    arm.halt(gid1)
    gid2 = arm.move_j((1, 1, 1, 1, 1, 1))
    arm.halt(gid1)  # stale
    assert arm._current_goal_id == gid2


def test_register_outputs_declares_expected_keys():
    arm = MockArm(name="m1")
    board = WorldBoard()
    arm.register_outputs(board)
    keys = set(board.registered_keys())
    assert keys == {
        "m1.pose",
        "m1.joint",
        "m1.running",
        "m1.enabled",
        "m1.error",
        "m1.safety_state",
        "m1.current_cmd_id",
    }


def test_publish_once_writes_world_board():
    arm = MockArm(name="m1")
    board = WorldBoard()
    arm.register_outputs(board)
    arm.publish_once()
    snap = board.snapshot()
    assert snap["m1.pose"] == (0.0,) * 6
    assert snap["m1.joint"] == (0.0,) * 6
    assert snap["m1.running"] is False
    assert snap["m1.enabled"] is True
    assert snap["m1.current_cmd_id"] == 0


def test_move_j_updates_joint_after_publish():
    arm = MockArm(name="m1", move_duration=0.0)
    board = WorldBoard()
    arm.register_outputs(board)
    arm.publish_once()  # initial state
    arm.move_j((1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
    arm.publish_once()  # arrives at target instantly
    snap = board.snapshot()
    assert snap["m1.joint"] == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    assert snap["m1.running"] is False


def test_halt_freezes_pose_at_current_value():
    arm = MockArm(name="m1", move_duration=10.0)
    board = WorldBoard()
    arm.register_outputs(board)
    gid = arm.move_l((9.0, 9.0, 9.0, 0.0, 0.0, 0.0))
    arm.publish_once()  # still in flight
    snap = board.snapshot()
    assert snap["m1.running"] is True
    arm.halt(gid)
    arm.publish_once()
    snap = board.snapshot()
    assert snap["m1.running"] is False
    # Pose did not jump to target because halt cancelled the goal.
    assert snap["m1.pose"] != (9.0, 9.0, 9.0, 0.0, 0.0, 0.0)


def test_lifecycle_thread_starts_and_stops():
    arm = MockArm(name="m1", feedback_hz=200.0)
    board = WorldBoard()
    arm.register_outputs(board)
    arm.start()
    try:
        time.sleep(0.05)
        assert board.snapshot().seq > 0
    finally:
        arm.stop()
    assert arm._fb_thread is None or not arm._fb_thread.is_alive()


def test_start_without_register_raises():
    arm = MockArm(name="m1")
    with pytest.raises(RuntimeError):
        arm.start()
