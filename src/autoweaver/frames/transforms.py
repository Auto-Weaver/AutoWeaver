"""Pure conversion functions: user-supplied pose data → standard 4×4 matrix.

Internal convention (target of all conversions):
  - translation in mm
  - 4×4 homogeneous matrix, right-handed
"""

from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

_QUAT_NORM_TOLERANCE = 1e-6

_RPY_CONVENTIONS: dict[str, tuple[str, bool]] = {
    # name -> (scipy seq, degrees)
    # scipy convention: uppercase = intrinsic, lowercase = extrinsic.
    "zyx_intrinsic_deg": ("ZYX", True),
    "zyx_intrinsic_rad": ("ZYX", False),
    "xyz_extrinsic_deg": ("xyz", True),
    "xyz_extrinsic_rad": ("xyz", False),
    "zyz_intrinsic_deg": ("ZYZ", True),
    "zyz_intrinsic_rad": ("ZYZ", False),
}


def supported_rpy_conventions() -> tuple[str, ...]:
    return tuple(_RPY_CONVENTIONS.keys())


def to_mm(xyz: list[float] | tuple[float, ...] | np.ndarray, unit: str) -> np.ndarray:
    arr = np.asarray(xyz, dtype=np.float64)
    if arr.shape != (3,):
        raise ValueError(f"xyz must have length 3, got shape {arr.shape}")
    if unit == "mm":
        return arr
    if unit == "m":
        return arr * 1000.0
    raise ValueError(f"unsupported xyz_unit: {unit!r} (expected 'mm' or 'm')")


def _validate_quat(q: np.ndarray) -> None:
    if q.shape != (4,):
        raise ValueError(f"quat must have length 4, got shape {q.shape}")
    norm = float(np.linalg.norm(q))
    if abs(norm - 1.0) > _QUAT_NORM_TOLERANCE:
        raise ValueError(
            f"quat is not unit-length: |q|={norm:.9f} "
            f"(tolerance ±{_QUAT_NORM_TOLERANCE})"
        )


def quat_to_matrix(
    xyz_mm: np.ndarray,
    quat: list[float] | tuple[float, ...] | np.ndarray,
    order: str,
) -> np.ndarray:
    """Build a 4×4 matrix from translation (already in mm) + quaternion.

    `order` is either 'xyzw' (scipy/ROS convention, default) or 'wxyz'.
    """
    q = np.asarray(quat, dtype=np.float64)
    if order == "xyzw":
        q_xyzw = q
    elif order == "wxyz":
        q_xyzw = np.array([q[1], q[2], q[3], q[0]], dtype=np.float64)
    else:
        raise ValueError(f"unsupported quat_order: {order!r} (expected 'xyzw' or 'wxyz')")

    _validate_quat(q_xyzw)
    rot = Rotation.from_quat(q_xyzw).as_matrix()
    return _compose(rot, xyz_mm)


def euler_to_matrix(
    xyz_mm: np.ndarray,
    rpy: list[float] | tuple[float, ...] | np.ndarray,
    convention: str,
) -> np.ndarray:
    """Build a 4×4 matrix from translation (already in mm) + Euler angles."""
    if convention not in _RPY_CONVENTIONS:
        raise ValueError(
            f"unsupported rpy_convention: {convention!r} "
            f"(supported: {sorted(_RPY_CONVENTIONS)})"
        )
    seq, degrees = _RPY_CONVENTIONS[convention]
    r = np.asarray(rpy, dtype=np.float64)
    if r.shape != (3,):
        raise ValueError(f"rpy must have length 3, got shape {r.shape}")
    rot = Rotation.from_euler(seq, r, degrees=degrees).as_matrix()
    return _compose(rot, xyz_mm)


def unwrap_euler(values: list[float] | tuple[float, ...] | np.ndarray) -> list[float]:
    """Make a sequence of Euler angles (degrees) continuous across the
    ±180° wrap-around boundary.

    Euler angles are only defined up to ±360°, so the same physical
    orientation can be reported as e.g. +179.999° or -179.999°. A teach
    pendant reading the same wrist pose at several waypoints can return a
    sequence that flips back and forth across the boundary::

        [-179.9996, +179.9994, +179.95, -179.97]

    Feeding that straight into interpolation (bilinear over corners, lerp
    over waypoints) or a ``move_l`` makes the controller see a ~360° wrist
    delta and either alarm on a joint limit or spin the wrist a full turn.

    This shifts each value by a multiple of 360° so consecutive entries
    never differ by more than 180° — the discrete analogue of
    ``numpy.unwrap`` for degrees, anchored on the first value::

        → [-179.9996, -180.0006, -180.05, -179.97]

    The first value is returned unchanged; only relative continuity matters.
    Apply independently to each of rx / ry / rz (see :func:`unwrap_poses`).
    """
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"values must be 1-D, got shape {arr.shape}")
    if arr.size == 0:
        return []
    out = [float(arr[0])]
    for v in arr[1:]:
        v = float(v)
        diff = v - out[-1]
        # Fold the step back into (-180, +180].
        v -= 360.0 * np.ceil((diff - 180.0) / 360.0)
        out.append(v)
    return out


def unwrap_poses(
    poses: list[list[float]] | list[tuple[float, ...]] | np.ndarray,
) -> list[list[float]]:
    """Unwrap the rotation channels of a sequence of 6-DOF poses.

    Each pose is ``(x, y, z, rx, ry, rz)`` with the rotation triplet in
    degrees. Translation is passed through untouched; rx / ry / rz are each
    run through :func:`unwrap_euler` so the sequence is continuous and safe
    to interpolate. Returns a new list of 6-element lists.
    """
    arr = np.asarray(poses, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] != 6:
        raise ValueError(
            f"poses must be shape (N, 6) = (x,y,z,rx,ry,rz), got {arr.shape}"
        )
    rx = unwrap_euler(arr[:, 3])
    ry = unwrap_euler(arr[:, 4])
    rz = unwrap_euler(arr[:, 5])
    return [
        [float(arr[i, 0]), float(arr[i, 1]), float(arr[i, 2]), rx[i], ry[i], rz[i]]
        for i in range(arr.shape[0])
    ]


def matrix_passthrough(matrix: list[list[float]] | np.ndarray) -> np.ndarray:
    """Validate a user-supplied 4×4 matrix and return it as float64.

    Caller is responsible for ensuring translation is already in mm.
    """
    m = np.asarray(matrix, dtype=np.float64)
    if m.shape != (4, 4):
        raise ValueError(f"matrix must be 4×4, got shape {m.shape}")
    if not np.allclose(m[3], [0.0, 0.0, 0.0, 1.0]):
        raise ValueError(f"matrix bottom row must be [0,0,0,1], got {m[3].tolist()}")
    # Rotation block must be a proper rotation: R R^T = I, det = +1.
    rot = m[:3, :3]
    if not np.allclose(rot @ rot.T, np.eye(3), atol=1e-6):
        raise ValueError("matrix upper-left 3×3 is not orthogonal")
    det = float(np.linalg.det(rot))
    if abs(det - 1.0) > 1e-6:
        raise ValueError(f"matrix rotation block has det={det:.9f}, expected +1")
    return m


def invert(matrix: np.ndarray) -> np.ndarray:
    """Closed-form inverse of a rigid 4×4 transform.

    Avoids `np.linalg.inv` because it's both slower and numerically less
    accurate for SE(3) than the analytic form.
    """
    rot = matrix[:3, :3]
    trans = matrix[:3, 3]
    inv = np.eye(4, dtype=np.float64)
    inv[:3, :3] = rot.T
    inv[:3, 3] = -rot.T @ trans
    return inv


def _compose(rot_3x3: np.ndarray, trans_mm: np.ndarray) -> np.ndarray:
    m = np.eye(4, dtype=np.float64)
    m[:3, :3] = rot_3x3
    m[:3, 3] = trans_mm
    return m
