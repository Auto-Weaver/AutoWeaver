from __future__ import annotations

import pytest

from autoweaver.device.arm.base import ArmBase
from autoweaver.device.arm.dobot import COORD_JOINT, COORD_POSE, Dobot
from autoweaver.motion_policy.world_board import WorldBoard


class _FakeDashboard:
    def __init__(self):
        self.calls: list[tuple] = []

    def MovJ(self, *args):
        self.calls.append(("MovJ", args))

    def MovL(self, *args):
        self.calls.append(("MovL", args))

    def Stop(self):
        self.calls.append(("Stop",))

    def close(self):
        self.calls.append(("close",))


def test_dobot_satisfies_arm_base_protocol():
    arm = Dobot(ip="127.0.0.1", name="d1")
    assert isinstance(arm, ArmBase)


def test_construction_does_not_open_sockets():
    """Dobot.__init__ must be side-effect-free."""
    arm = Dobot(ip="127.0.0.1", name="d1")
    assert arm._dashboard is None
    assert arm._feedback is None


def test_register_outputs_declares_expected_keys():
    arm = Dobot(ip="127.0.0.1", name="d1")
    board = WorldBoard()
    arm.register_outputs(board)
    assert set(board.registered_keys()) == {
        "d1.pose",
        "d1.joint",
        "d1.running",
        "d1.enabled",
        "d1.error",
        "d1.safety_state",
        "d1.current_cmd_id",
    }


def test_move_j_uses_joint_mode_by_default():
    arm = Dobot(ip="127.0.0.1", name="d1")
    arm._dashboard = _FakeDashboard()
    gid = arm.move_j((10, 20, 30, 40, 50, 60))
    assert gid == 1
    name, args = arm._dashboard.calls[0]
    assert name == "MovJ"
    assert args[-1] == COORD_JOINT
    assert args[:6] == (10.0, 20.0, 30.0, 40.0, 50.0, 60.0)


def test_move_j_pose_mode_when_configured():
    arm = Dobot(ip="127.0.0.1", name="d1", joint_coord_mode=False)
    arm._dashboard = _FakeDashboard()
    arm.move_j((1, 2, 3, 4, 5, 6))
    args = arm._dashboard.calls[0][1]
    assert args[-1] == COORD_POSE


def test_move_l_always_uses_pose_mode():
    arm = Dobot(ip="127.0.0.1", name="d1", joint_coord_mode=True)
    arm._dashboard = _FakeDashboard()
    arm.move_l((1, 2, 3, 4, 5, 6))
    name, args = arm._dashboard.calls[0]
    assert name == "MovL"
    assert args[-1] == COORD_POSE


def test_move_before_start_raises():
    arm = Dobot(ip="127.0.0.1", name="d1")
    with pytest.raises(RuntimeError):
        arm.move_j((0, 0, 0, 0, 0, 0))


def test_halt_current_goal_calls_sdk_stop():
    arm = Dobot(ip="127.0.0.1", name="d1")
    arm._dashboard = _FakeDashboard()
    gid = arm.move_j((0, 0, 0, 0, 0, 0))
    arm.halt(gid)
    assert ("Stop",) in arm._dashboard.calls
    assert arm._current_goal_id is None


def test_halt_stale_goal_is_ignored():
    arm = Dobot(ip="127.0.0.1", name="d1")
    arm._dashboard = _FakeDashboard()
    gid1 = arm.move_j((0, 0, 0, 0, 0, 0))
    arm.halt(gid1)  # clears _current_goal_id
    arm._dashboard.calls.clear()
    gid2 = arm.move_j((1, 1, 1, 1, 1, 1))
    arm.halt(gid1)  # stale — must not call Stop
    stops = [c for c in arm._dashboard.calls if c[0] == "Stop"]
    assert stops == []
    assert arm._current_goal_id == gid2


def test_move_rejects_wrong_arity():
    arm = Dobot(ip="127.0.0.1", name="d1")
    arm._dashboard = _FakeDashboard()
    with pytest.raises(ValueError):
        arm.move_j((1, 2, 3))


def test_start_without_register_raises():
    arm = Dobot(ip="127.0.0.1", name="d1")
    with pytest.raises(RuntimeError):
        arm.start()
