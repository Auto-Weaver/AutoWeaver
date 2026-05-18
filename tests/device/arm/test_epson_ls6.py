from __future__ import annotations

import math

import numpy as np
import pytest

from autoweaver.device.arm.base import ArmBase4
from autoweaver.device.arm.epson_ls6.driver import EpsonLS6
from autoweaver.motion_policy.mock_runtime_client import MockRuntimeClient


# ─── Protocol conformance ──────────────────────────────────────────────────


def test_epson_ls6_satisfies_arm_base_4_protocol():
    arm = EpsonLS6(MockRuntimeClient(), device_name="ls6_1", name="ls6_1")
    assert isinstance(arm, ArmBase4)


def test_dof_is_four():
    arm = EpsonLS6(MockRuntimeClient(), device_name="ls6_1", name="ls6_1")
    assert arm.dof == 4


# ─── move_l / move_j / jump send the right motion ──────────────────────────


def test_move_l_records_linear_with_4_tuple_target():
    client = MockRuntimeClient()
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1")
    gid = arm.move_l((100.0, 200.0, 50.0, 90.0))
    assert gid == 1
    assert client.goals == [
        (
            "scara",
            "ls6_1",
            "LINEAR",
            {"x": 100.0, "y": 200.0, "z": 50.0, "u": 90.0, "speed": 50, "accel": 200},
        ),
    ]


def test_move_j_records_go():
    client = MockRuntimeClient()
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1")
    arm.move_j((1.0, 2.0, 3.0, 4.0))
    assert client.goals[0][2] == "GO"


def test_jump_records_jump():
    client = MockRuntimeClient()
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1")
    arm.jump((1.0, 2.0, 3.0, 4.0))
    assert client.goals[0][2] == "JUMP"


# ─── Goal id is monotonic ──────────────────────────────────────────────────


def test_goal_ids_increment_across_calls():
    client = MockRuntimeClient()
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1")
    gid1 = arm.move_l((1.0, 0.0, 0.0, 0.0))
    gid2 = arm.move_j((2.0, 0.0, 0.0, 0.0))
    gid3 = arm.jump((3.0, 0.0, 0.0, 0.0))
    assert (gid1, gid2, gid3) == (1, 2, 3)


# ─── Target validation ─────────────────────────────────────────────────────


def test_move_l_rejects_3_tuple():
    arm = EpsonLS6(MockRuntimeClient(), device_name="ls6_1", name="ls6_1")
    with pytest.raises(ValueError, match="4 elements"):
        arm.move_l((1.0, 2.0, 3.0))


def test_move_l_rejects_6_tuple():
    arm = EpsonLS6(MockRuntimeClient(), device_name="ls6_1", name="ls6_1")
    with pytest.raises(ValueError, match="4 elements"):
        arm.move_l((1.0, 2.0, 3.0, 0.0, 0.0, 0.0))


def test_move_j_rejects_wrong_arity():
    arm = EpsonLS6(MockRuntimeClient(), device_name="ls6_1", name="ls6_1")
    with pytest.raises(ValueError, match="4 elements"):
        arm.move_j((1, 2, 3, 4, 5))


def test_jump_rejects_wrong_arity():
    arm = EpsonLS6(MockRuntimeClient(), device_name="ls6_1", name="ls6_1")
    with pytest.raises(ValueError, match="4 elements"):
        arm.jump((1, 2, 3))


# ─── speed / accel defaults and overrides ──────────────────────────────────


def test_default_speed_accel_come_from_constructor():
    client = MockRuntimeClient()
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1", speed=80, accel=150)
    arm.move_l((1.0, 2.0, 3.0, 4.0))
    fields = client.goals[0][3]
    assert fields["speed"] == 80
    assert fields["accel"] == 150


def test_per_call_speed_overrides_default():
    client = MockRuntimeClient()
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1", speed=50, accel=200)
    arm.move_l((1.0, 2.0, 3.0, 4.0), speed=10)
    fields = client.goals[0][3]
    assert fields["speed"] == 10
    # accel keeps default
    assert fields["accel"] == 200


def test_per_call_accel_overrides_default():
    client = MockRuntimeClient()
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1", speed=50, accel=200)
    arm.move_l((1.0, 2.0, 3.0, 4.0), accel=500)
    fields = client.goals[0][3]
    assert fields["accel"] == 500
    assert fields["speed"] == 50


def test_speed_override_works_for_move_j_and_jump():
    client = MockRuntimeClient()
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1")
    arm.move_j((0.0, 0.0, 0.0, 0.0), speed=15)
    arm.jump((1.0, 0.0, 0.0, 0.0), speed=25)
    assert client.goals[0][3]["speed"] == 15
    assert client.goals[1][3]["speed"] == 25


# ─── get_flange_pose ───────────────────────────────────────────────────────


def test_get_flange_pose_returns_4x4_matrix():
    client = MockRuntimeClient()
    client.preload_scara_status("ls6_1", current_x=0.0, current_y=0.0, current_z=0.0, current_u=0.0)
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1")
    m = arm.get_flange_pose()
    assert m.shape == (4, 4)
    assert np.allclose(m, np.eye(4))


def test_get_flange_pose_translation_matches_status():
    client = MockRuntimeClient()
    client.preload_scara_status(
        "ls6_1", current_x=100.0, current_y=200.0, current_z=50.0, current_u=0.0,
    )
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1")
    m = arm.get_flange_pose()
    assert np.allclose(m[:3, 3], [100.0, 200.0, 50.0])


def test_get_flange_pose_yaw_rotation_matches_u():
    """u = 90° → rotation matrix should rotate the world X axis to world Y."""
    client = MockRuntimeClient()
    client.preload_scara_status("ls6_1", current_u=90.0)
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1")
    m = arm.get_flange_pose()
    # World x-hat → flange direction after 90° yaw rotation = world y-hat.
    world_x = np.array([1.0, 0.0, 0.0])
    rotated = m[:3, :3] @ world_x
    assert np.allclose(rotated, [0.0, 1.0, 0.0], atol=1e-10)


def test_get_flange_pose_no_pitch_or_roll_component():
    """SCARA pose only has yaw — pitch / roll components of the matrix
    must be zero regardless of u."""
    client = MockRuntimeClient()
    client.preload_scara_status("ls6_1", current_u=45.0)
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1")
    m = arm.get_flange_pose()
    # Z column (third column of rotation) must remain [0, 0, 1] — no tilt.
    assert np.allclose(m[:3, 2], [0.0, 0.0, 1.0])
    # cos(45°) / sin(45°) appear in the upper 2×2 block.
    c = math.cos(math.radians(45.0))
    s = math.sin(math.radians(45.0))
    assert np.allclose(m[0, 0], c)
    assert np.allclose(m[1, 0], s)


# ─── halt / lifecycle ──────────────────────────────────────────────────────


def test_halt_is_no_op_pre_next_011():
    """Halt protocol is deferred to NEXT-011; calling halt() must not raise."""
    arm = EpsonLS6(MockRuntimeClient(), device_name="ls6_1", name="ls6_1")
    gid = arm.move_l((1.0, 2.0, 3.0, 4.0))
    arm.halt(gid)  # must not raise


def test_halt_with_stale_goal_is_also_no_op():
    """Even stale goal ids must not raise (Protocol semantics)."""
    arm = EpsonLS6(MockRuntimeClient(), device_name="ls6_1", name="ls6_1")
    arm.halt(99999)  # never issued


def test_start_and_stop_are_no_ops():
    """RuntimeClient lifecycle is owned by the Worker, not the driver."""
    client = MockRuntimeClient()
    arm = EpsonLS6(client, device_name="ls6_1", name="ls6_1")
    arm.start()
    arm.stop()
    # Driver remains usable across start/stop because nothing is opened/closed.
    arm.move_l((1.0, 0.0, 0.0, 0.0))
    assert client.goals[0][2] == "LINEAR"


# ─── Multiple devices share one client ─────────────────────────────────────


def test_two_drivers_on_same_client_do_not_collide():
    """Two EpsonLS6 instances pointing at different device names use the
    same RuntimeClient but produce independent goal streams."""
    client = MockRuntimeClient()
    arm1 = EpsonLS6(client, device_name="ls6_1", name="arm1")
    arm2 = EpsonLS6(client, device_name="ls6_2", name="arm2")
    arm1.move_l((1.0, 0.0, 0.0, 0.0))
    arm2.move_j((2.0, 0.0, 0.0, 0.0))
    devices = [g[1] for g in client.goals]
    assert devices == ["ls6_1", "ls6_2"]
