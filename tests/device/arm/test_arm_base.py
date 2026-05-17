from __future__ import annotations

import pytest

from autoweaver.device.arm.base import (
    validate_joint_target,
    validate_target_4dof,
    validate_target_6dof,
)


# ─── validate_target_4dof ──────────────────────────────────────────────────


def test_validate_target_4dof_accepts_4_tuple():
    out = validate_target_4dof((10.0, 20.0, 30.0, 90.0), "ls6_1")
    assert out == (10.0, 20.0, 30.0, 90.0)


def test_validate_target_4dof_coerces_ints_to_floats():
    out = validate_target_4dof((10, 20, 30, 90), "ls6_1")
    assert out == (10.0, 20.0, 30.0, 90.0)
    assert all(isinstance(x, float) for x in out)


def test_validate_target_4dof_rejects_short():
    with pytest.raises(ValueError, match="4 elements"):
        validate_target_4dof((1.0, 2.0, 3.0), "ls6_1")


def test_validate_target_4dof_rejects_long():
    with pytest.raises(ValueError, match="4 elements"):
        validate_target_4dof((1.0, 2.0, 3.0, 4.0, 5.0), "ls6_1")


def test_validate_target_4dof_error_mentions_arm_name():
    with pytest.raises(ValueError, match="ls6_1"):
        validate_target_4dof((1.0,), "ls6_1")


# ─── validate_target_6dof ──────────────────────────────────────────────────


def test_validate_target_6dof_accepts_6_tuple():
    out = validate_target_6dof((10.0, 20.0, 30.0, 0.0, 0.0, 90.0), "dobot1")
    assert out == (10.0, 20.0, 30.0, 0.0, 0.0, 90.0)


def test_validate_target_6dof_rejects_4_tuple():
    with pytest.raises(ValueError, match="6 elements"):
        validate_target_6dof((1.0, 2.0, 3.0, 4.0), "dobot1")


def test_validate_target_6dof_rejects_7_tuple():
    with pytest.raises(ValueError, match="6 elements"):
        validate_target_6dof((1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0), "dobot1")


# ─── validate_joint_target ─────────────────────────────────────────────────


def test_validate_joint_target_accepts_correct_length():
    out = validate_joint_target((10, 20, 30, 40, 50, 60), 6, "d1")
    assert out == (10.0, 20.0, 30.0, 40.0, 50.0, 60.0)


def test_validate_joint_target_rejects_wrong_length():
    with pytest.raises(ValueError, match="6-DOF"):
        validate_joint_target((10, 20, 30), 6, "d1")
