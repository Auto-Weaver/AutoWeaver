import numpy as np
import pytest

from autoweaver.geometry import transforms


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
