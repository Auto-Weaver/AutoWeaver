from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

import numpy as np


GoalId = int

# Tolerance for the "rx/ry must be 0" check on 4-DOF arms. Accommodates
# upstream float noise (e.g. matrix products that come out 1e-15 instead
# of exactly 0); any intentional non-zero tilt is far above this.
_TILT_TOLERANCE_DEG = 1e-3


def validate_cartesian_target(
    target: Sequence[float], dof: int, arm_name: str
) -> tuple[float, float, float, float, float, float]:
    """Validate a 6-element Cartesian target ``(x, y, z, rx, ry, rz)``.

    Always rejects wrong length. For 4-DOF arms (SCARA-like: only x/y/z
    translation and yaw via rz), additionally rejects non-zero rx/ry —
    they're physically unreachable, so honoring such a target silently
    would mean lying about the pose.
    """
    target_tuple = tuple(float(x) for x in target)
    if len(target_tuple) != 6:
        raise ValueError(
            f"{arm_name}: Cartesian target must have 6 elements (x,y,z,rx,ry,rz), "
            f"got {len(target_tuple)}"
        )
    x, y, z, rx, ry, rz = target_tuple
    if dof == 4 and (abs(rx) > _TILT_TOLERANCE_DEG or abs(ry) > _TILT_TOLERANCE_DEG):
        raise ValueError(
            f"{arm_name}: 4-DOF arm cannot tilt — target has rx={rx}°, ry={ry}° "
            f"(both must be 0). This arm only supports x/y/z translation and rz (yaw); "
            f"set rx=0 and ry=0 in your move_l/move_j target."
        )
    return (x, y, z, rx, ry, rz)


def validate_joint_target(
    target: Sequence[float], dof: int, arm_name: str
) -> tuple[float, ...]:
    """Validate a joint-angle target. Length must equal the arm's DOF."""
    target_tuple = tuple(float(x) for x in target)
    if len(target_tuple) != dof:
        raise ValueError(
            f"{arm_name}: joint target must have {dof} elements "
            f"(this arm is {dof}-DOF), got {len(target_tuple)}"
        )
    return target_tuple


@runtime_checkable
class ArmBase(Protocol):
    """Common interface every robot arm must satisfy.

    Control methods are fire-and-forget at the task level: they send a
    goal to the controller and return a GoalId immediately. Communication-
    level synchronous waiting (e.g. TCP RPC ACK) is expected and
    acceptable as long as it stays well within the BT tick budget.

    Three motion primitives matching the universal industrial-arm core
    (Dobot MovJ/MovL, KUKA PTP/LIN, ABB MoveJ/MoveL/MoveAbsJ, etc.):

      - ``move_j`` — Cartesian target, joint-interpolated path (PTP).
        Tool tip follows whatever curve falls out of the joint motion.
      - ``move_l`` — Cartesian target, Cartesian-linear path. Tool tip
        moves in a straight line; controller does continuous IK.
      - ``move_j_joints`` — Joint-angle target, joint-interpolated path.
        Skips IK entirely; useful for unambiguous home/service poses.

    The ``j`` / ``l`` letters describe path shape, not target format.
    Cartesian targets are always 6-tuples ``(x, y, z, rx, ry, rz)`` in
    mm + degrees (ZYX intrinsic). Joint-target length equals ``dof``
    (6 for typical industrial arms, 4 for SCARA).

    ``dof`` declares the arm's reachable axes. 4-DOF SCARA-like arms
    have x/y/z translation and yaw (rz) only — Cartesian targets with
    non-zero rx/ry are rejected at the driver entry. 6-DOF arms accept
    any orientation.

    Feedback is pull-style (NEXT-008): leaves call ``get_flange_pose()``
    on demand. The driver is responsible for converting the SDK's native
    pose representation into a standard 4×4 matrix; leaves never see the
    SDK's Euler convention or unit choices.
    """

    name: str
    dof: int

    # --- control (fire-and-forget) ---

    def move_j(self, target: Sequence[float]) -> GoalId:
        """PTP to a Cartesian target. Returns a goal id usable with ``halt()``."""
        ...

    def move_l(self, target: Sequence[float]) -> GoalId:
        """Linear move to a Cartesian target. Returns a goal id usable with ``halt()``."""
        ...

    def move_j_joints(self, target: Sequence[float]) -> GoalId:
        """PTP to a joint-angle target. Returns a goal id usable with ``halt()``."""
        ...

    def halt(self, goal_id: GoalId) -> None:
        """Stop the goal identified by ``goal_id`` if it is still current.

        Stale halts (the goal already finished or was superseded) are
        silently ignored — they must not interrupt a newer goal.
        """
        ...

    # --- feedback (pull) ---

    def get_flange_pose(self) -> np.ndarray:
        """Return T(base ← flange) as a 4×4 matrix, translation in mm.

        Pulled on demand from the SDK. Must be called after ``start()``.
        """
        ...

    # --- lifecycle ---

    def start(self) -> None:
        """Connect (if needed) and prepare for control / feedback access."""
        ...

    def stop(self) -> None:
        """Disconnect and release resources."""
        ...
