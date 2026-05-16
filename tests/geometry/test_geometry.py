import textwrap
from pathlib import Path

import numpy as np
import pytest

from autoweaver.geometry import Geometry


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cal.yaml"
    path.write_text(textwrap.dedent(body).lstrip())
    return path


# ---------------------------------------------------------------------------
# Loading + dict population
# ---------------------------------------------------------------------------

def test_world_relative_frames_indexed(tmp_path):
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
          - name: fixture_tray
            parent: world
            xyz: [500, 500, 50]
            rpy: [0, 0, 0]
    """)
    g = Geometry(path)
    assert np.allclose(g.world_from("arm_1_base"), np.eye(4))
    assert np.allclose(g.world_from("arm_2_base")[:3, 3], [1200, 0, 0])
    assert np.allclose(g.world_from("fixture_tray")[:3, 3], [500, 500, 50])


def test_flange_relative_tools_indexed(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_tool_camera
            parent: arm_1_flange
            xyz: [50, 0, 100]
            rpy: [0, 0, 0]
          - name: arm_2_tool_gripper
            parent: arm_2_flange
            xyz: [0, 0, 150]
            rpy: [0, 0, 0]
    """)
    g = Geometry(path)
    assert np.allclose(g.flange_from("arm_1_tool_camera")[:3, 3], [50, 0, 100])
    assert np.allclose(g.flange_from("arm_2_tool_gripper")[:3, 3], [0, 0, 150])


# ---------------------------------------------------------------------------
# Inverses
# ---------------------------------------------------------------------------

def test_base_from_world_inverts_world_from_base(tmp_path):
    """Use a non-trivial rotation (90° about Z) so the inverse test is real."""
    path = _write(tmp_path, """
        frames:
          - name: arm_2_base
            parent: world
            xyz: [1200, 300, 0]
            rpy: [90, 0, 0]
    """)
    g = Geometry(path)
    product = g.world_from("arm_2_base") @ g.base_from_world("arm_2_base")
    assert np.allclose(product, np.eye(4), atol=1e-9)


def test_tool_from_flange_inverts_flange_from(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_tool_camera
            parent: arm_1_flange
            xyz: [50, 0, 100]
            rpy: [0, -90, 0]
    """)
    g = Geometry(path)
    product = g.flange_from("arm_1_tool_camera") @ g.tool_from_flange("arm_1_tool_camera")
    assert np.allclose(product, np.eye(4), atol=1e-9)


# ---------------------------------------------------------------------------
# flange_of
# ---------------------------------------------------------------------------

def test_flange_of_returns_owning_flange(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_tool_camera
            parent: arm_1_flange
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
          - name: arm_2_tool_gripper
            parent: arm_2_flange
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
    """)
    g = Geometry(path)
    assert g.flange_of("arm_1_tool_camera") == "arm_1_flange"
    assert g.flange_of("arm_2_tool_gripper") == "arm_2_flange"


# ---------------------------------------------------------------------------
# Lookup error messages — must steer the user, not just "KeyError"
# ---------------------------------------------------------------------------

def test_world_from_on_tool_explains_use_flange_from(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_tool_camera
            parent: arm_1_flange
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
    """)
    g = Geometry(path)
    with pytest.raises(KeyError, match="flange_from"):
        g.world_from("arm_1_tool_camera")


def test_flange_from_on_base_explains_use_world_from(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
    """)
    g = Geometry(path)
    with pytest.raises(KeyError, match="world_from"):
        g.flange_from("arm_1_base")


def test_world_from_on_flange_explains_dynamic(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
    """)
    g = Geometry(path)
    with pytest.raises(KeyError, match="dynamic"):
        g.world_from("arm_1_flange")


def test_missing_frame_lists_what_is_loaded(tmp_path):
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
    """)
    g = Geometry(path)
    with pytest.raises(KeyError, match="arm_1_base"):
        g.world_from("arm_99_base")


# ---------------------------------------------------------------------------
# Composition: typical multi-arm hand-off math
# ---------------------------------------------------------------------------

def test_point_in_cam_to_arm2_base_via_world(tmp_path):
    """End-to-end: a point seen by arm_1's camera, expressed in arm_2's base.

    Static segments come from Geometry; the dynamic flange poses are
    supplied by the test (in real life they come from the SDK).
    """
    path = _write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
          - name: arm_2_base
            parent: world
            xyz: [1000, 0, 0]
            rpy: [0, 0, 0]
          - name: arm_1_tool_camera
            parent: arm_1_flange
            xyz: [0, 0, 50]
            rpy: [0, 0, 0]
    """)
    g = Geometry(path)

    # arm_1 flange currently at (500, 0, 200) in its own base, no rotation.
    T_base1_flange1 = np.eye(4)
    T_base1_flange1[:3, 3] = [500, 0, 200]

    # Point at the camera origin (so 0,0,0 in camera frame).
    point_cam_homo = np.array([0, 0, 0, 1.0])

    T_world_cam = (
        g.world_from("arm_1_base")
        @ T_base1_flange1
        @ g.flange_from("arm_1_tool_camera")
    )
    point_world = T_world_cam @ point_cam_homo
    # Expected: arm_1_base at world origin, flange at (500,0,200), camera +50 on z.
    assert np.allclose(point_world[:3], [500, 0, 250])

    # Now flip the same world point into arm_2's base frame.
    T_base2_world = g.base_from_world("arm_2_base")
    point_in_base2 = T_base2_world @ point_world
    # arm_2_base is 1000mm along +x from world origin → point should be (-500, 0, 250).
    assert np.allclose(point_in_base2[:3], [-500, 0, 250])
