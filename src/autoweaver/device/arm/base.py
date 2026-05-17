from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

import numpy as np


GoalId = int


def validate_target_4dof(
    target: Sequence[float], arm_name: str
) -> tuple[float, float, float, float]:
    """Validate a 4-element SCARA Cartesian target ``(x, y, z, u)``.

    ``u`` is wrist yaw (equivalent to ``rz`` in 6-DOF notation). SCARA
    arms physically have no ``rx`` / ``ry`` degrees of freedom — those
    axes simply don't exist on the kinematic chain — so the target shape
    stays at 4 instead of carrying constant-zero entries.
    """
    target_tuple = tuple(float(x) for x in target)
    if len(target_tuple) != 4:
        raise ValueError(
            f"{arm_name}: 4-DOF Cartesian target must have 4 elements "
            f"(x, y, z, u), got {len(target_tuple)}"
        )
    x, y, z, u = target_tuple
    return (x, y, z, u)


def validate_target_6dof(
    target: Sequence[float], arm_name: str
) -> tuple[float, float, float, float, float, float]:
    """Validate a 6-element Cartesian target ``(x, y, z, rx, ry, rz)``."""
    target_tuple = tuple(float(x) for x in target)
    if len(target_tuple) != 6:
        raise ValueError(
            f"{arm_name}: 6-DOF Cartesian target must have 6 elements "
            f"(x, y, z, rx, ry, rz), got {len(target_tuple)}"
        )
    x, y, z, rx, ry, rz = target_tuple
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
class ArmBase4(Protocol):
    """SCARA arm: 4-DOF (x, y, z translation + u/yaw rotation).

    Target shape is a 4-tuple ``(x, y, z, u)`` in mm + degrees. No
    ``rx`` / ``ry`` because the arm has no axes for them.

    SCARA-specific method: ``jump`` — composite pick-place motion (raise
    Z to a clearance plane → translate XY → lower Z onto target). All
    major SCARA brands (Epson, Yamaha, Mitsubishi) expose an equivalent
    primitive; 6-DOF arms have no native equivalent.

    Control methods are fire-and-forget at the task level: they send a
    goal to the controller and return a GoalId immediately. Push-side
    state (done / busy / pose / ...) is published to WorldBoard by the
    arm's Worker, not by this driver.

    Feedback note: ``get_flange_pose`` is a *direct* pose read for
    scripts and debugging. Inside a BT, leaves read the pose from
    ``snapshot["<arm>.pose"]`` instead — that path is maintained by the
    arm's Worker and reflects the current published state.
    """

    name: str
    dof: int  # = 4 for implementations

    def move_j(self, target: Sequence[float]) -> GoalId: ...
    def move_l(self, target: Sequence[float]) -> GoalId: ...
    def jump(self, target: Sequence[float]) -> GoalId: ...
    def halt(self, goal_id: GoalId) -> None: ...
    def get_flange_pose(self) -> np.ndarray: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


@runtime_checkable
class ArmBase6(Protocol):
    """6-DOF industrial arm: full Cartesian ``(x, y, z, rx, ry, rz)``.

    Target shape is a 6-tuple in mm + degrees (ZYX intrinsic).

    Three motion primitives matching the universal industrial-arm core
    (Dobot MovJ/MovL, KUKA PTP/LIN, ABB MoveJ/MoveL/MoveAbsJ, etc.):

      - ``move_j`` — Cartesian target, joint-interpolated path (PTP).
      - ``move_l`` — Cartesian target, Cartesian-linear path.
      - ``move_j_joints`` — Joint-angle target, joint-interpolated path.
        Skips IK entirely; useful for unambiguous home/service poses.

    Control methods are fire-and-forget at the task level. Push-side
    state (done / busy / pose / ...) is published to WorldBoard by the
    arm's Worker, not by this driver.
    """

    name: str
    dof: int  # = 6 for implementations

    def move_j(self, target: Sequence[float]) -> GoalId: ...
    def move_l(self, target: Sequence[float]) -> GoalId: ...
    def move_j_joints(self, target: Sequence[float]) -> GoalId: ...
    def halt(self, goal_id: GoalId) -> None: ...
    def get_flange_pose(self) -> np.ndarray: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
