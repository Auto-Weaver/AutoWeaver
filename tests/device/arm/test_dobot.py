from __future__ import annotations

import numpy as np
import pytest

from autoweaver.device.arm.base import ArmBase
from autoweaver.device.arm.dobot import COORD_JOINT, COORD_POSE, Dobot


class _FakeDashboard:
    def __init__(self):
        self.calls: list[tuple] = []

    def MovJ(self, *args, **kwargs):
        self.calls.append(("MovJ", args, kwargs))

    def MovL(self, *args, **kwargs):
        self.calls.append(("MovL", args, kwargs))

    def Stop(self):
        self.calls.append(("Stop",))

    def close(self):
        self.calls.append(("close",))


class _FakeFeedback:
    """Returns a single canned feedback frame on every read."""

    def __init__(self, frame: dict):
        self._frame = frame
        self.read_count = 0

    def feedBackData(self):
        self.read_count += 1
        return [self._frame]

    def close(self):
        pass


def _frame_with_pose(pose: tuple[float, ...]) -> dict:
    """Construct a minimal feedback frame holding the given pose tuple."""
    return {"ToolVectorActual": pose}


def test_dobot_satisfies_arm_base_protocol():
    arm = Dobot(ip="127.0.0.1", name="d1")
    assert isinstance(arm, ArmBase)


def test_construction_does_not_open_sockets():
    """Dobot.__init__ must be side-effect-free."""
    arm = Dobot(ip="127.0.0.1", name="d1")
    assert arm._dashboard is None
    assert arm._feedback is None


def test_move_j_uses_joint_mode_by_default():
    arm = Dobot(ip="127.0.0.1", name="d1")
    arm._dashboard = _FakeDashboard()
    gid = arm.move_j((10, 20, 30, 40, 50, 60))
    assert gid == 1
    name, args, _kwargs = arm._dashboard.calls[0]
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
    name, args, _kwargs = arm._dashboard.calls[0]
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


def test_move_j_omits_speed_by_default():
    """Without speed=, SDK call uses v=-1 (i.e. SDK's "use default" sentinel)."""
    arm = Dobot(ip="127.0.0.1", name="d1")
    arm._dashboard = _FakeDashboard()
    arm.move_j((1, 2, 3, 4, 5, 6))
    _name, _args, kwargs = arm._dashboard.calls[0]
    assert kwargs == {"v": -1}


def test_move_j_passes_speed_through():
    arm = Dobot(ip="127.0.0.1", name="d1")
    arm._dashboard = _FakeDashboard()
    arm.move_j((1, 2, 3, 4, 5, 6), speed=10)
    _name, _args, kwargs = arm._dashboard.calls[0]
    assert kwargs == {"v": 10}


def test_move_l_passes_speed_through():
    arm = Dobot(ip="127.0.0.1", name="d1")
    arm._dashboard = _FakeDashboard()
    arm.move_l((1, 2, 3, 4, 5, 6), speed=25)
    _name, _args, kwargs = arm._dashboard.calls[0]
    assert kwargs == {"v": 25}


def test_move_rejects_speed_out_of_range():
    arm = Dobot(ip="127.0.0.1", name="d1")
    arm._dashboard = _FakeDashboard()
    with pytest.raises(ValueError):
        arm.move_j((1, 2, 3, 4, 5, 6), speed=0)
    with pytest.raises(ValueError):
        arm.move_j((1, 2, 3, 4, 5, 6), speed=101)


# ─── get_flange_pose (NEXT-008) ────────────────────────────────────────────


def test_get_flange_pose_translates_sdk_frame_to_matrix():
    """ToolVectorActual = (x, y, z, rx, ry, rz) in mm + degrees (ZYX intrinsic)
    must turn into a 4×4 matrix with translation in mm."""
    arm = Dobot(ip="127.0.0.1", name="d1")
    arm._feedback = _FakeFeedback(_frame_with_pose((100.0, 200.0, 300.0, 0.0, 0.0, 0.0)))
    m = arm.get_flange_pose()
    assert m.shape == (4, 4)
    assert np.allclose(m[:3, 3], [100.0, 200.0, 300.0])
    assert np.allclose(m[:3, :3], np.eye(3))


def test_get_flange_pose_90deg_z_rotates_x_axis_to_y_axis():
    arm = Dobot(ip="127.0.0.1", name="d1")
    # rx (yaw, first ZYX axis) = 90° → rotates +X to +Y
    arm._feedback = _FakeFeedback(_frame_with_pose((0.0, 0.0, 0.0, 90.0, 0.0, 0.0)))
    m = arm.get_flange_pose()
    rotated_x = m[:3, :3] @ np.array([1.0, 0.0, 0.0])
    assert np.allclose(rotated_x, [0.0, 1.0, 0.0], atol=1e-9)


def test_get_flange_pose_without_start_raises():
    arm = Dobot(ip="127.0.0.1", name="d1")
    with pytest.raises(RuntimeError, match="start"):
        arm.get_flange_pose()
