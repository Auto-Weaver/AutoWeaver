import textwrap
from pathlib import Path

import numpy as np
import pytest

from autoweaver.frames import schema
from autoweaver.frames.schema import CalibrationSchemaError


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
            rpy: [0, 0, 0]
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
            rpy: [0, 0, 0]
          - name: arm_2_base
            parent: world
            xyz: [1200, 0, 0]
            rpy: [0, 0, 0]
          - name: arm_1_tool_camera
            parent: arm_1_flange
            xyz: [50, 0, 100]
            rpy: [0, 0, 0]
          - name: fixture_tray_a
            parent: world
            xyz: [800, 400, 50]
            rpy: [0, 0, 0]
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
# Name / parent: no naming convention — arbitrary names accepted
# ---------------------------------------------------------------------------

def test_arbitrary_names_accepted(tmp_path):
    """Naming convention was removed — any non-empty name/parent is fine."""
    path = _write(tmp_path, """
        frames:
          - name: my_robot
            parent: shop_floor
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
          - name: weird.frame-name
            parent: my_robot
            xyz: [1, 2, 3]
            rpy: [0, 0, 0]
    """)
    edges = schema.load(path)
    assert {e.name for e in edges} == {"my_robot", "weird.frame-name"}
    assert edges[1].parent == "my_robot"


def test_empty_name_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: ""
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
    """)
    with pytest.raises(CalibrationSchemaError, match="non-empty"):
        schema.load(path)


def test_duplicate_name_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
          - name: arm_1_base
            parent: world
            xyz: [100, 0, 0]
            rpy: [0, 0, 0]
    """)
    with pytest.raises(CalibrationSchemaError, match="duplicate"):
        schema.load(path)


# ---------------------------------------------------------------------------
# Dynamic edges
# ---------------------------------------------------------------------------

def test_dynamic_edge_parsed(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_flange
            parent: arm_1_base
            dynamic:
              state_key: arm_1.pose
              required: true
    """)
    edges = schema.load(path)
    e = edges[0]
    assert e.is_dynamic
    assert e.matrix is None
    assert e.state_key == "arm_1.pose"
    assert e.required is True


def test_dynamic_required_defaults_false(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_flange_corrected
            parent: arm_1_flange
            dynamic:
              state_key: droop.arm_1
    """)
    edges = schema.load(path)
    assert edges[0].required is False


def test_dynamic_with_static_fields_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_flange
            parent: arm_1_base
            xyz: [0, 0, 0]
            dynamic:
              state_key: arm_1.pose
    """)
    with pytest.raises(CalibrationSchemaError, match="must not also carry"):
        schema.load(path)


def test_dynamic_missing_state_key_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_flange
            parent: arm_1_base
            dynamic:
              required: true
    """)
    with pytest.raises(CalibrationSchemaError, match="state_key"):
        schema.load(path)


def test_dynamic_unknown_subfield_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_flange
            parent: arm_1_base
            dynamic:
              state_key: arm_1.pose
              freqency: 50
    """)
    with pytest.raises(CalibrationSchemaError, match="unknown 'dynamic' fields"):
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


def test_rpy_and_matrix_together_rejected(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
            matrix:
              - [1, 0, 0, 0]
              - [0, 1, 0, 0]
              - [0, 0, 1, 0]
              - [0, 0, 0, 1]
    """)
    with pytest.raises(CalibrationSchemaError, match="mutually exclusive"):
        schema.load(path)


# ---------------------------------------------------------------------------
# Rotation parsing
# ---------------------------------------------------------------------------

def test_rpy_zero_yields_identity_rotation(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [10, 20, 30]
            rpy: [0, 0, 0]
    """)
    edges = schema.load(path)
    assert np.allclose(edges[0].matrix[:3, :3], np.eye(3))
    assert np.allclose(edges[0].matrix[:3, 3], [10, 20, 30])


def test_rpy_90deg_first_axis_rotates_x_to_y(tmp_path):
    """ZYX intrinsic: the first angle is yaw about Z. 90° about Z sends +X to +Y."""
    path = _write(tmp_path, """
        frames:
          - name: arm_1_tool_camera
            parent: arm_1_flange
            xyz: [50, 0, 100]
            rpy: [90, 0, 0]
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
            rpy: [0, 0, 0]
            color: red
    """)
    with pytest.raises(CalibrationSchemaError, match="unknown fields"):
        schema.load(path)


def test_typo_quaternion_caught(tmp_path):
    """Quaternions are no longer supported — but the whitelist still catches
    the typo so the user gets a clear schema error instead of silent fallback."""
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            quaternion: [0, 0, 0, 1]
    """)
    with pytest.raises(CalibrationSchemaError, match="unknown fields"):
        schema.load(path)


def test_quat_field_no_longer_accepted(tmp_path):
    """`quat` was the old rotation field; it's now rejected at the whitelist."""
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            quat: [0, 0, 0, 1]
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
            rpy: [0, 0, 0]
          - name: arm_2_base
            parent: world
            xyz: [0, 0, 0]
            bogus_field: 1
    """)
    with pytest.raises(CalibrationSchemaError, match="arm_2_base"):
        schema.load(path)
