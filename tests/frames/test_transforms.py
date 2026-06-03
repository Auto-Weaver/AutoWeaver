import numpy as np
import pytest

from autoweaver.frames import transforms


# ---------------------------------------------------------------------------
# to_mm
# ---------------------------------------------------------------------------

def test_to_mm_default_unit_passes_through():
    out = transforms.to_mm([1.0, 2.0, 3.0], "mm")
    assert np.allclose(out, [1.0, 2.0, 3.0])


def test_to_mm_meters_scales_by_1000():
    out = transforms.to_mm([0.5, 0.0, 0.1], "m")
    assert np.allclose(out, [500.0, 0.0, 100.0])


def test_to_mm_rejects_unknown_unit():
    with pytest.raises(ValueError, match="xyz_unit"):
        transforms.to_mm([1.0, 2.0, 3.0], "inch")


def test_to_mm_rejects_wrong_shape():
    with pytest.raises(ValueError, match="length 3"):
        transforms.to_mm([1.0, 2.0], "mm")


# ---------------------------------------------------------------------------
# quat_to_matrix
# ---------------------------------------------------------------------------

def test_quat_identity_xyzw_is_identity_rotation():
    m = transforms.quat_to_matrix(np.array([10.0, 20.0, 30.0]), [0, 0, 0, 1], "xyzw")
    assert np.allclose(m[:3, :3], np.eye(3))
    assert np.allclose(m[:3, 3], [10.0, 20.0, 30.0])
    assert np.allclose(m[3], [0, 0, 0, 1])


def test_quat_wxyz_and_xyzw_agree_when_reordered():
    xyz_mm = np.array([0.0, 0.0, 0.0])
    # 90° rotation about Z: xyzw = [0, 0, sin(45°), cos(45°)]
    s = np.sin(np.pi / 4)
    c = np.cos(np.pi / 4)
    m_xyzw = transforms.quat_to_matrix(xyz_mm, [0, 0, s, c], "xyzw")
    m_wxyz = transforms.quat_to_matrix(xyz_mm, [c, 0, 0, s], "wxyz")
    assert np.allclose(m_xyzw, m_wxyz)


def test_quat_90deg_z_rotates_x_axis_to_y_axis():
    xyz_mm = np.array([0.0, 0.0, 0.0])
    s = np.sin(np.pi / 4)
    c = np.cos(np.pi / 4)
    m = transforms.quat_to_matrix(xyz_mm, [0, 0, s, c], "xyzw")
    x_axis = np.array([1.0, 0.0, 0.0])
    rotated = m[:3, :3] @ x_axis
    assert np.allclose(rotated, [0.0, 1.0, 0.0], atol=1e-9)


def test_quat_non_unit_length_rejected():
    with pytest.raises(ValueError, match="unit-length"):
        transforms.quat_to_matrix(np.zeros(3), [0, 0, 0, 1.1], "xyzw")


def test_quat_unknown_order_rejected():
    with pytest.raises(ValueError, match="quat_order"):
        transforms.quat_to_matrix(np.zeros(3), [0, 0, 0, 1], "wxyzw")


# ---------------------------------------------------------------------------
# euler_to_matrix
# ---------------------------------------------------------------------------

def test_euler_zero_is_identity():
    m = transforms.euler_to_matrix(np.array([1.0, 2.0, 3.0]), [0, 0, 0], "zyx_intrinsic_deg")
    assert np.allclose(m[:3, :3], np.eye(3))
    assert np.allclose(m[:3, 3], [1.0, 2.0, 3.0])


def test_euler_zyx_intrinsic_deg_90_around_z():
    m = transforms.euler_to_matrix(np.zeros(3), [90, 0, 0], "zyx_intrinsic_deg")
    rotated = m[:3, :3] @ np.array([1.0, 0.0, 0.0])
    assert np.allclose(rotated, [0.0, 1.0, 0.0], atol=1e-9)


def test_euler_rad_and_deg_agree():
    m_deg = transforms.euler_to_matrix(np.zeros(3), [45, 0, 0], "zyx_intrinsic_deg")
    m_rad = transforms.euler_to_matrix(np.zeros(3), [np.pi / 4, 0, 0], "zyx_intrinsic_rad")
    assert np.allclose(m_deg, m_rad)


def test_euler_unsupported_convention_rejected():
    with pytest.raises(ValueError, match="rpy_convention"):
        transforms.euler_to_matrix(np.zeros(3), [0, 0, 0], "made_up")


# ---------------------------------------------------------------------------
# matrix_passthrough
# ---------------------------------------------------------------------------

def test_matrix_passthrough_accepts_valid():
    m = np.eye(4)
    m[:3, 3] = [10, 20, 30]
    out = transforms.matrix_passthrough(m)
    assert np.allclose(out, m)
    assert out.dtype == np.float64


def test_matrix_passthrough_rejects_wrong_shape():
    with pytest.raises(ValueError, match="4×4"):
        transforms.matrix_passthrough(np.eye(3))


def test_matrix_passthrough_rejects_bad_bottom_row():
    m = np.eye(4)
    m[3, 0] = 1
    with pytest.raises(ValueError, match="bottom row"):
        transforms.matrix_passthrough(m)


def test_matrix_passthrough_rejects_non_orthogonal_rotation():
    m = np.eye(4)
    m[0, 0] = 2.0  # scale, not rotation
    with pytest.raises(ValueError, match="orthogonal"):
        transforms.matrix_passthrough(m)


def test_matrix_passthrough_rejects_reflection():
    m = np.eye(4)
    m[0, 0] = -1  # det = -1 (mirror)
    with pytest.raises(ValueError, match="det"):
        transforms.matrix_passthrough(m)


# ---------------------------------------------------------------------------
# invert
# ---------------------------------------------------------------------------

def test_invert_identity_is_identity():
    inv = transforms.invert(np.eye(4))
    assert np.allclose(inv, np.eye(4))


def test_invert_matches_np_linalg_inv_for_rigid_transform():
    m = transforms.quat_to_matrix(
        np.array([10.0, 20.0, 30.0]),
        [0, 0, np.sin(np.pi / 6), np.cos(np.pi / 6)],
        "xyzw",
    )
    inv = transforms.invert(m)
    expected = np.linalg.inv(m)
    assert np.allclose(inv, expected, atol=1e-12)


def test_invert_then_compose_yields_identity():
    m = transforms.quat_to_matrix(
        np.array([10.0, -5.0, 7.0]),
        [0.1, 0.2, 0.3, 0.9273618495495704],  # normalized
        "xyzw",
    )
    assert np.allclose(m @ transforms.invert(m), np.eye(4), atol=1e-12)


# ---------------------------------------------------------------------------
# unwrap_euler / unwrap_poses
# ---------------------------------------------------------------------------

def test_unwrap_euler_doc_example():
    """The motivating teach-pendant sequence from NEXT-007."""
    out = transforms.unwrap_euler([-179.9996, 179.9994, 179.95, -179.97])
    assert np.allclose(out, [-179.9996, -180.0006, -180.05, -179.97])
    # Every consecutive step is now ≤ 180° in magnitude.
    diffs = np.diff(out)
    assert np.all(np.abs(diffs) <= 180.0)


def test_unwrap_euler_first_value_unchanged():
    out = transforms.unwrap_euler([170.0, -170.0])
    assert out[0] == 170.0
    # 170 → -170 is a -340 raw step; unwrap folds it to +20.
    assert np.isclose(out[1], 190.0)


def test_unwrap_euler_no_wrap_passthrough():
    vals = [0.0, 10.0, 20.0, 30.0]
    assert np.allclose(transforms.unwrap_euler(vals), vals)


def test_unwrap_euler_empty_and_single():
    assert transforms.unwrap_euler([]) == []
    assert transforms.unwrap_euler([42.0]) == [42.0]


def test_unwrap_euler_rejects_2d():
    with pytest.raises(ValueError, match="1-D"):
        transforms.unwrap_euler([[1.0, 2.0], [3.0, 4.0]])


def test_unwrap_poses_passes_translation_unwraps_rotation():
    poses = [
        [0, 0, 0, 0, 0, -179.9996],
        [1, 2, 3, 0, 0, 179.9994],
    ]
    out = transforms.unwrap_poses(poses)
    # translation untouched
    assert out[1][:3] == [1.0, 2.0, 3.0]
    # rz unwrapped to stay continuous
    assert np.isclose(out[1][5], -180.0006)


def test_unwrap_poses_rejects_wrong_shape():
    with pytest.raises(ValueError, match=r"\(N, 6\)"):
        transforms.unwrap_poses([[0, 0, 0, 0, 0]])  # only 5 columns

