import textwrap
from pathlib import Path

import numpy as np
import pytest

from autoweaver.frames import (
    Frames,
    FrameNotFound,
    FramesDisconnected,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cal.yaml"
    path.write_text(textwrap.dedent(body).lstrip())
    return path


# A two-arm cell used by several tests: arm_1 at world origin with a camera,
# arm_2 1000mm down +x with a gripper.
_TWO_ARM = """
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
      - name: arm_2_tool_gripper
        parent: arm_2_flange
        xyz: [0, 0, 0]
        rpy: [0, 0, 0]
"""


def _translation(x, y, z) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = [x, y, z]
    return m


# ---------------------------------------------------------------------------
# Static-only lookup
# ---------------------------------------------------------------------------

def test_identity_lookup(tmp_path):
    f = Frames(_write(tmp_path, _TWO_ARM))
    assert np.allclose(f.lookup("arm_1_base", "arm_1_base"), np.eye(4))


def test_static_forward_lookup(tmp_path):
    f = Frames(_write(tmp_path, _TWO_ARM))
    # T(world ← arm_2_base): arm_2_base sits at (1000,0,0) in world.
    assert np.allclose(f.lookup("world", "arm_2_base")[:3, 3], [1000, 0, 0])


def test_static_reverse_lookup_inverts(tmp_path):
    """The reverse direction must invert the stored edge on the fly."""
    f = Frames(_write(tmp_path, _TWO_ARM))
    fwd = f.lookup("world", "arm_2_base")
    rev = f.lookup("arm_2_base", "world")
    assert np.allclose(fwd @ rev, np.eye(4), atol=1e-9)
    assert np.allclose(rev[:3, 3], [-1000, 0, 0])


def test_static_multi_hop_across_world(tmp_path):
    """arm_2_base ← arm_1_base goes through world (two static hops)."""
    f = Frames(_write(tmp_path, _TWO_ARM))
    T = f.lookup("arm_2_base", "arm_1_base")
    # arm_1 at origin, arm_2 at +1000x → arm_1 origin is at -1000x in arm_2.
    assert np.allclose(T[:3, 3], [-1000, 0, 0])


# ---------------------------------------------------------------------------
# Dynamic edges
# ---------------------------------------------------------------------------

def test_dynamic_edge_value_from_snapshot(tmp_path):
    f = Frames(_write(tmp_path, _TWO_ARM))
    f.bind_dynamic("arm_1_base", "arm_1_flange",
                   state_key="arm_1.pose", required=True)
    # flange at (500, 0, 200) in arm_1_base.
    snap = {"arm_1.pose": _translation(500, 0, 200)}
    T = f.lookup("arm_1_base", "arm_1_flange", snap)
    assert np.allclose(T[:3, 3], [500, 0, 200])


def test_dynamic_chain_camera_point_to_world(tmp_path):
    """Forward: a point at the camera origin, expressed in world."""
    f = Frames(_write(tmp_path, _TWO_ARM))
    f.bind_dynamic("arm_1_base", "arm_1_flange",
                   state_key="arm_1.pose", required=True)
    snap = {"arm_1.pose": _translation(500, 0, 200)}
    # camera is +50z on the flange; flange at (500,0,200) → camera at (500,0,250).
    p_world = f.transform_point([0, 0, 0], "arm_1_tool_camera", "world", snap)
    assert np.allclose(p_world, [500, 0, 250])


def test_cross_arm_lookup_uses_one_snapshot(tmp_path):
    """The headline use case: arm_1's camera point in arm_2's gripper frame,
    both arms' poses read from a single consistent snapshot."""
    f = Frames(_write(tmp_path, _TWO_ARM))
    f.bind_dynamic("arm_1_base", "arm_1_flange",
                   state_key="arm_1.pose", required=True)
    f.bind_dynamic("arm_2_base", "arm_2_flange",
                   state_key="arm_2.pose", required=True)
    snap = {
        "arm_1.pose": _translation(500, 0, 200),
        "arm_2.pose": _translation(0, 0, 300),
    }
    # camera origin in world = (500, 0, 250) as above.
    # arm_2 gripper = arm_2 flange (no offset), flange at (0,0,300) in arm_2_base,
    # arm_2_base at (1000,0,0) in world → gripper at (1000,0,300) in world.
    # camera point in gripper frame = (500-1000, 0, 250-300) = (-500, 0, -50).
    p = f.transform_point([0, 0, 0], "arm_1_tool_camera", "arm_2_tool_gripper", snap)
    assert np.allclose(p, [-500, 0, -50])


def test_dynamic_edge_reverse_inverts_live_value(tmp_path):
    """flange ← base must invert the live pose (the 'on-the-fly inverse')."""
    f = Frames(_write(tmp_path, _TWO_ARM))
    f.bind_dynamic("arm_1_base", "arm_1_flange",
                   state_key="arm_1.pose", required=True)
    snap = {"arm_1.pose": _translation(500, 0, 200)}
    fwd = f.lookup("arm_1_base", "arm_1_flange", snap)
    rev = f.lookup("arm_1_flange", "arm_1_base", snap)
    assert np.allclose(fwd @ rev, np.eye(4), atol=1e-9)


# ---------------------------------------------------------------------------
# Missing-value policy: required vs optional
# ---------------------------------------------------------------------------

def test_required_dynamic_edge_missing_raises(tmp_path):
    f = Frames(_write(tmp_path, _TWO_ARM))
    f.bind_dynamic("arm_1_base", "arm_1_flange",
                   state_key="arm_1.pose", required=True)
    with pytest.raises(FramesDisconnected, match="load-bearing"):
        f.lookup("arm_1_base", "arm_1_flange", {})  # no arm_1.pose


def test_optional_dynamic_edge_missing_is_identity(tmp_path):
    """An optional compensation edge absent from the snapshot is a no-op."""
    f = Frames(_write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
    """))
    # droop correction sits between flange and a corrected flange frame.
    f.bind_dynamic("arm_1_flange", "arm_1_flange_corrected",
                   state_key="droop.arm_1", required=False)
    T = f.lookup("arm_1_flange", "arm_1_flange_corrected", {})  # no droop value
    assert np.allclose(T, np.eye(4))


def test_optional_edge_applies_when_present(tmp_path):
    f = Frames(_write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
    """))
    f.bind_dynamic("arm_1_flange", "arm_1_flange_corrected",
                   state_key="droop.arm_1", required=False)
    snap = {"droop.arm_1": _translation(0, 0, -3)}  # 3mm sag
    T = f.lookup("arm_1_flange", "arm_1_flange_corrected", snap)
    assert np.allclose(T[:3, 3], [0, 0, -3])


# ---------------------------------------------------------------------------
# Structural failures
# ---------------------------------------------------------------------------

def test_unknown_frame_raises_frame_not_found(tmp_path):
    f = Frames(_write(tmp_path, _TWO_ARM))
    with pytest.raises(FrameNotFound, match="arm_99_base"):
        f.lookup("world", "arm_99_base")


def test_flange_unreferenced_explains_bind(tmp_path):
    """A frame named nowhere isn't a graph node — the error lists what is
    known and explains how a frame comes to exist."""
    f = Frames(_write(tmp_path, """
        frames:
          - name: arm_1_base
            parent: world
            xyz: [0, 0, 0]
            rpy: [0, 0, 0]
    """))
    with pytest.raises(FrameNotFound, match="not in the graph"):
        f.lookup("world", "arm_1_flange")


def test_flange_present_but_unbridged_disconnects(tmp_path):
    """When a tool hangs off the flange, the flange exists as a node — but
    without the flange-pose edge it's an island, so world ← flange has no
    path (FramesDisconnected, not FrameNotFound)."""
    f = Frames(_write(tmp_path, _TWO_ARM))
    with pytest.raises(FramesDisconnected):
        f.lookup("world", "arm_1_flange")


def test_disconnected_components_raise(tmp_path):
    """Camera hangs off a flange that's never bridged to world → no path."""
    f = Frames(_write(tmp_path, _TWO_ARM))
    # arm_1_tool_camera is under arm_1_flange, which is not connected to world
    # until the flange-pose edge is bound. So world ← camera has no path.
    with pytest.raises(FramesDisconnected):
        f.lookup("world", "arm_1_tool_camera")


def test_double_bind_same_edge_raises(tmp_path):
    f = Frames(_write(tmp_path, _TWO_ARM))
    f.bind_dynamic("arm_1_base", "arm_1_flange",
                   state_key="arm_1.pose", required=True)
    with pytest.raises(ValueError, match="already bound"):
        f.bind_dynamic("arm_1_base", "arm_1_flange",
                       state_key="arm_1.other", required=True)


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

def test_describe_path_flags_dynamic_hops(tmp_path):
    f = Frames(_write(tmp_path, _TWO_ARM))
    f.bind_dynamic("arm_1_base", "arm_1_flange",
                   state_key="arm_1.pose", required=True)
    hops = f.describe_path("world", "arm_1_tool_camera")
    dynamic_hops = [h for h in hops if h["dynamic"]]
    assert len(dynamic_hops) == 1
    assert dynamic_hops[0]["state_key"] == "arm_1.pose"


def test_can_lookup_reflects_missing_required(tmp_path):
    f = Frames(_write(tmp_path, _TWO_ARM))
    f.bind_dynamic("arm_1_base", "arm_1_flange",
                   state_key="arm_1.pose", required=True)
    assert not f.can_lookup("world", "arm_1_tool_camera", {})
    snap = {"arm_1.pose": np.eye(4)}
    assert f.can_lookup("world", "arm_1_tool_camera", snap)


# ---------------------------------------------------------------------------
# Dynamic edges declared in YAML (not bound in code)
# ---------------------------------------------------------------------------

# A full cell where the flange-pose dynamic edge and a droop compensation
# edge are declared in the YAML itself.
_CELL_WITH_DYNAMIC = """
    frames:
      - name: arm_1_base
        parent: world
        xyz: [0, 0, 0]
        rpy: [0, 0, 0]
      - name: arm_1_flange
        parent: arm_1_base
        dynamic:
          state_key: arm_1.pose
          required: true
      - name: arm_1_flange_corrected
        parent: arm_1_flange
        dynamic:
          state_key: droop.arm_1        # optional: missing → identity
      - name: arm_1_tool_gripper
        parent: arm_1_flange_corrected
        xyz: [0, 0, 100]
        rpy: [0, 0, 0]
"""


def test_yaml_dynamic_edge_full_chain(tmp_path):
    f = Frames(_write(tmp_path, _CELL_WITH_DYNAMIC))
    # flange at (500,0,200); droop not published yet → corrected == flange.
    snap = {"arm_1.pose": _translation(500, 0, 200)}
    # gripper is +100z on the corrected flange → (500, 0, 300) in world.
    p = f.transform_point([0, 0, 0], "arm_1_tool_gripper", "world", snap)
    assert np.allclose(p, [500, 0, 300])


def test_yaml_optional_droop_applies_when_present(tmp_path):
    f = Frames(_write(tmp_path, _CELL_WITH_DYNAMIC))
    snap = {
        "arm_1.pose": _translation(500, 0, 200),
        "droop.arm_1": _translation(0, 0, -5),  # 5mm sag at the flange
    }
    p = f.transform_point([0, 0, 0], "arm_1_tool_gripper", "world", snap)
    assert np.allclose(p, [500, 0, 295])  # 300 - 5


def test_yaml_required_flange_missing_raises(tmp_path):
    f = Frames(_write(tmp_path, _CELL_WITH_DYNAMIC))
    with pytest.raises(FramesDisconnected, match="load-bearing"):
        f.transform_point([0, 0, 0], "arm_1_tool_gripper", "world", {})
