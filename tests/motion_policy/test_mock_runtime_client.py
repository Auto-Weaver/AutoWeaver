from __future__ import annotations

import pytest

from autoweaver.motion_policy.mock_runtime_client import MockRuntimeClient
from autoweaver.motion_policy.runtime_client import GoalError


# ---------------------------------------------------------------------------
# SCARA goals
# ---------------------------------------------------------------------------


def test_scara_linear_records_motion_and_target():
    client = MockRuntimeClient()
    (
        client.scara_goal("ls6_1")
        .linear(x=100.0, y=200.0, z=50.0, u=0.0)
        .speed(50)
        .accel(200)
        .submit()
    )
    assert client.goals == [
        (
            "scara",
            "ls6_1",
            "LINEAR",
            {"x": 100.0, "y": 200.0, "z": 50.0, "u": 0.0, "speed": 50, "accel": 200},
        ),
    ]


def test_scara_home_records_motion_without_target():
    client = MockRuntimeClient()
    client.scara_goal("ls6_1").home().speed(10).submit()
    assert client.goals == [("scara", "ls6_1", "HOME", {"speed": 10})]


def test_scara_submit_without_motion_raises():
    client = MockRuntimeClient()
    with pytest.raises(GoalError, match="no motion type set"):
        client.scara_goal("ls6_1").speed(50).submit()
    assert client.goals == []


def test_submit_sets_busy_and_clears_done():
    client = MockRuntimeClient()
    client.preload_scara_status("ls6_1", done=True, busy=False)
    client.scara_goal("ls6_1").linear(x=1.0, y=2.0, z=3.0, u=0.0).submit()
    status = client.read_scara_status("ls6_1")
    assert status.done is False
    assert status.busy is True
    assert status.error_code == 0


def test_complete_last_goal_flips_status_to_done():
    client = MockRuntimeClient()
    client.scara_goal("ls6_1").linear(x=1.0, y=2.0, z=3.0, u=0.0).submit()
    client.complete_last_goal("ls6_1")
    status = client.read_scara_status("ls6_1")
    assert status.done is True
    assert status.busy is False


# ---------------------------------------------------------------------------
# Status reads
# ---------------------------------------------------------------------------


def test_read_status_for_unknown_device_raises():
    client = MockRuntimeClient()
    with pytest.raises(GoalError, match="unknown device"):
        client.read_scara_status("ls6_1")


def test_preload_status_seeds_without_recording_goal():
    client = MockRuntimeClient()
    client.preload_scara_status("ls6_1", current_x=42.0, done=True)
    status = client.read_scara_status("ls6_1")
    assert status.current_x == 42.0
    assert status.done is True
    assert client.goals == []  # preload doesn't record


# ---------------------------------------------------------------------------
# Multiple devices
# ---------------------------------------------------------------------------


def test_devices_have_independent_status():
    client = MockRuntimeClient()
    client.scara_goal("ls6_1").linear(x=100.0, y=0.0, z=0.0, u=0.0).submit()
    client.preload_scara_status("ls6_2", done=True, current_x=999.0)
    assert client.read_scara_status("ls6_1").busy is True
    assert client.read_scara_status("ls6_1").done is False
    assert client.read_scara_status("ls6_2").done is True
    assert client.read_scara_status("ls6_2").current_x == 999.0


# ---------------------------------------------------------------------------
# 6-DOF
# ---------------------------------------------------------------------------


def test_arm6_linear_records_motion_and_all_six_components():
    client = MockRuntimeClient()
    (
        client.arm6_goal("nova_1")
        .linear(x=1.0, y=2.0, z=3.0, rx=10.0, ry=20.0, rz=30.0)
        .speed(60)
        .submit()
    )
    assert client.goals == [
        (
            "arm6",
            "nova_1",
            "LINEAR",
            {
                "x": 1.0,
                "y": 2.0,
                "z": 3.0,
                "rx": 10.0,
                "ry": 20.0,
                "rz": 30.0,
                "speed": 60,
            },
        ),
    ]


def test_arm6_status_independent_from_scara():
    client = MockRuntimeClient()
    client.preload_arm6_status("nova_1", current_rz=90.0, done=True)
    status = client.read_arm6_status("nova_1")
    assert status.current_rz == 90.0
    assert status.done is True


def test_arm6_submit_without_motion_raises():
    client = MockRuntimeClient()
    with pytest.raises(GoalError, match="no motion type set"):
        client.arm6_goal("nova_1").speed(50).submit()


# ---------------------------------------------------------------------------
# Goal recording order is preserved
# ---------------------------------------------------------------------------


def test_goal_recording_preserves_submission_order():
    client = MockRuntimeClient()
    client.scara_goal("ls6_1").linear(x=1.0, y=0.0, z=0.0, u=0.0).submit()
    client.scara_goal("ls6_1").go(x=2.0, y=0.0, z=0.0, u=0.0).submit()
    client.scara_goal("ls6_1").home().submit()
    motion_names = [entry[2] for entry in client.goals]
    assert motion_names == ["LINEAR", "GO", "HOME"]


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_context_manager_returns_self():
    with MockRuntimeClient() as client:
        assert isinstance(client, MockRuntimeClient)
        client.scara_goal("dev").home().submit()
