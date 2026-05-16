import textwrap
from pathlib import Path

import numpy as np
import pytest

from autoweaver.geometry import schema
from autoweaver.geometry.schema import CalibrationSchemaError


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cal.yaml"
    path.write_text(textwrap.dedent(body).lstrip())
    return path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_minimal_valid_file(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0.0, 0.0, 0.0]
            quat: [0, 0, 0, 1]
    """)
    edges = schema.load(path)
    assert len(edges) == 1
    e = edges[0]
    assert e.name == "arm_1_base"
    assert e.parent == "world"
    assert np.allclose(e.matrix, np.eye(4))


def test_multiple_frames_with_tools(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
          - name: arm_2_base
            parent: world
            xyz: [1200, 0, 0]
            quat: [0, 0, 0, 1]
          - name: arm_1_tool_camera
            parent: arm_1_flange
            xyz: [50, 0, 100]
            quat: [0, 0, 0, 1]
          - name: fixture_tray_a
            parent: world
            xyz: [800, 400, 50]
            quat: [0, 0, 0, 1]
    """)
    edges = schema.load(path)
    assert {e.name for e in edges} == {
        "arm_1_base", "arm_2_base", "arm_1_tool_camera", "fixture_tray_a",
    }


# ---------------------------------------------------------------------------
# Top-level structure
# ---------------------------------------------------------------------------

def test_missing_frames_key_rejected(tmp_path):
    path = _write(tmp_path, "other: 1\n")
    with pytest.raises(CalibrationSchemaError, match="'frames'"):
        schema.load(path)


def test_frames_must_be_list(tmp_path):
    path = _write(tmp_path, "frames: not_a_list\n")
    with pytest.raises(CalibrationSchemaError, match="must be a list"):
        schema.load(path)


def test_yaml_parse_error_wrapped(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("frames: [\n  - name: x\n")  # unterminated
    with pytest.raises(CalibrationSchemaError, match="failed to parse"):
        schema.load(path)


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

def test_world_name_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: world
            parent: world
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
    """)
    with pytest.raises(CalibrationSchemaError, match="implicit root"):
        schema.load(path)


def test_flange_as_name_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_flange
            parent: world
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
    """)
    with pytest.raises(CalibrationSchemaError, match="dynamic"):
        schema.load(path)


def test_arbitrary_name_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: my_robot
            parent: world
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
    """)
    with pytest.raises(CalibrationSchemaError, match="does not match"):
        schema.load(path)


def test_duplicate_name_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
          - name: arm_1_base
            parent: world
            xyz: [100, 0, 0]
            quat: [0, 0, 0, 1]
    """)
    with pytest.raises(CalibrationSchemaError, match="duplicate"):
        schema.load(path)


# ---------------------------------------------------------------------------
# Parent validation
# ---------------------------------------------------------------------------

def test_parent_not_world_or_flange_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_tool_camera
            parent: arm_1_base
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
    """)
    with pytest.raises(CalibrationSchemaError, match="parent"):
        schema.load(path)


# ---------------------------------------------------------------------------
# Rotation field validation
# ---------------------------------------------------------------------------

def test_no_rotation_field_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
    """)
    with pytest.raises(CalibrationSchemaError, match="rotation"):
        schema.load(path)


def test_two_rotation_fields_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
            rpy: [0, 0, 0]
            rpy_convention: zyx_intrinsic_deg
    """)
    with pytest.raises(CalibrationSchemaError, match="mutually exclusive"):
        schema.load(path)


def test_rpy_without_convention_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
    """)
    with pytest.raises(CalibrationSchemaError, match="rpy_convention"):
        schema.load(path)


def test_rpy_with_unsupported_convention_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
            rpy_convention: made_up
    """)
    with pytest.raises(CalibrationSchemaError, match="rpy_convention"):
        schema.load(path)


# ---------------------------------------------------------------------------
# Switch fields produce the expected matrix
# ---------------------------------------------------------------------------

def test_xyz_unit_meters_converted_to_mm(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0.5, 0.0, 0.1]
            xyz_unit: m
            quat: [0, 0, 0, 1]
    """)
    edges = schema.load(path)
    assert np.allclose(edges[0].matrix[:3, 3], [500.0, 0.0, 100.0])


def test_quat_order_wxyz_parsed(tmp_path):
    # 90° about Z written in wxyz order.
    s = float(np.sin(np.pi / 4))
    c = float(np.cos(np.pi / 4))
    path = _write(tmp_path, f"""
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            quat: [{c}, 0, 0, {s}]
            quat_order: wxyz
    """)
    edges = schema.load(path)
    rotated_x = edges[0].matrix[:3, :3] @ np.array([1.0, 0.0, 0.0])
    assert np.allclose(rotated_x, [0.0, 1.0, 0.0], atol=1e-9)


def test_rpy_zyx_intrinsic_deg_parsed(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_tool_camera
            parent: arm_1_flange
            xyz: [50, 0, 100]
            rpy: [90, 0, 0]
            rpy_convention: zyx_intrinsic_deg
    """)
    edges = schema.load(path)
    rotated_x = edges[0].matrix[:3, :3] @ np.array([1.0, 0.0, 0.0])
    assert np.allclose(rotated_x, [0.0, 1.0, 0.0], atol=1e-9)


def test_matrix_field_used_directly(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: fixture_tray_a
            parent: world
            matrix:
              - [1, 0, 0, 800]
              - [0, 1, 0, 400]
              - [0, 0, 1, 50]
              - [0, 0, 0, 1]
    """)
    edges = schema.load(path)
    expected = np.eye(4)
    expected[:3, 3] = [800, 400, 50]
    assert np.allclose(edges[0].matrix, expected)


def test_matrix_with_xyz_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: fixture_tray_a
            parent: world
            xyz: [800, 400, 50]
            matrix:
              - [1, 0, 0, 0]
              - [0, 1, 0, 0]
              - [0, 0, 1, 0]
              - [0, 0, 0, 1]
    """)
    with pytest.raises(CalibrationSchemaError, match="self-contained"):
        schema.load(path)


# ---------------------------------------------------------------------------
# Field whitelist
# ---------------------------------------------------------------------------

def test_unknown_field_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
            color: red
    """)
    with pytest.raises(CalibrationSchemaError, match="unknown fields"):
        schema.load(path)


def test_typo_quaternion_caught(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            quaternion: [0, 0, 0, 1]
    """)
    with pytest.raises(CalibrationSchemaError, match="unknown fields"):
        schema.load(path)


# ---------------------------------------------------------------------------
# Error message context
# ---------------------------------------------------------------------------

def test_error_includes_frame_name_context(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
          - name: arm_2_base
            parent: floor
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
    """)
    with pytest.raises(CalibrationSchemaError, match="arm_2_base"):
        schema.load(path)
