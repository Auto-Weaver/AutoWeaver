from __future__ import annotations

from typing import Sequence

import numpy as np

from autoweaver.device.arm.base import GoalId, validate_target_4dof
from autoweaver.geometry import transforms
from autoweaver.motion_policy.runtime_client import RuntimeClient


# SCARA pose convention: (x, y, z, u) where u is yaw about the world Z axis.
# Encoded as ZYX-intrinsic Euler with rx=ry=0 so leaves see the same 4×4
# matrix shape as Dobot / MockArm — only the rotation axis content differs.
_POSE_RPY_CONVENTION = "zyx_intrinsic_deg"


class EpsonLS6:
    """Epson LS6 SCARA arm via motion-runtime gRPC. Conforms to ArmBase4.

    Driver is a *thin* control wrapper: every ArmBase4 method translates
    one-to-one into a ``RuntimeClient.scara_goal(...).<motion>(...)`` builder
    call. State observation (done / busy / pose / joints) is *not* on this
    class — it is published to the WorldBoard by ``EpsonLS6Worker``. Inside
    a BT, leaves read the published state via ``snapshot["<arm>.done"]``
    etc.; ``get_flange_pose`` here is a direct-read fallback for scripts
    and debugging.

    Construction is side-effect-free (matches every other ArmBase driver).
    The runtime channel is owned by the Worker, so ``start`` / ``stop``
    are no-ops for this driver.

    Defaults for ``speed`` / ``accel`` are applied to every motion call;
    each call may override via kwargs.

    SCARA-specific ``jump`` (raise Z → translate XY → lower Z) is part of
    ArmBase4 and routes to the SPEL+ ``Jump`` primitive. 6-DOF arms have
    no equivalent.

    Halt currently no-ops — the goal/halt loop is deferred to NEXT-011
    (proto needs ``GoalResponse.goal_id`` and a halt RPC first). The
    method exists so the type checker accepts EpsonLS6 as ArmBase4.
    """

    dof = 4

    def __init__(
        self,
        client: RuntimeClient,
        device_name: str,
        name: str,
        *,
        speed: int = 50,
        accel: int = 200,
    ):
        self.name = name
        self._client = client
        self._device_name = device_name
        self._speed = speed
        self._accel = accel
        self._goal_counter: GoalId = 0

    # --- control ---

    def move_j(
        self,
        target: Sequence[float],
        *,
        speed: int | None = None,
        accel: int | None = None,
    ) -> GoalId:
        x, y, z, u = validate_target_4dof(target, self.name)
        self._submit_motion("go", x, y, z, u, speed, accel)
        return self._next_goal_id()

    def move_l(
        self,
        target: Sequence[float],
        *,
        speed: int | None = None,
        accel: int | None = None,
    ) -> GoalId:
        x, y, z, u = validate_target_4dof(target, self.name)
        self._submit_motion("linear", x, y, z, u, speed, accel)
        return self._next_goal_id()

    def jump(
        self,
        target: Sequence[float],
        *,
        speed: int | None = None,
        accel: int | None = None,
    ) -> GoalId:
        x, y, z, u = validate_target_4dof(target, self.name)
        self._submit_motion("jump", x, y, z, u, speed, accel)
        return self._next_goal_id()

    def halt(self, goal_id: GoalId) -> None:
        # NEXT-011: halt protocol pending. proto GoalResponse has no
        # goal_id field yet, so the runtime cannot match this halt to a
        # specific in-flight goal. No-op until that lands.
        return

    # --- feedback (direct read) ---

    def get_flange_pose(self) -> np.ndarray:
        status = self._client.read_scara_status(self._device_name)
        return _scara_status_to_matrix(status)

    # --- lifecycle ---

    def start(self) -> None:
        # RuntimeClient lifecycle is owned by EpsonLS6Worker.
        return

    def stop(self) -> None:
        return

    # --- internals ---

    def _submit_motion(
        self,
        motion_kind: str,
        x: float,
        y: float,
        z: float,
        u: float,
        speed: int | None,
        accel: int | None,
    ) -> None:
        builder = self._client.scara_goal(self._device_name)
        # motion_kind ∈ {"go", "linear", "jump"} → ScaraGoalBuilder methods
        getattr(builder, motion_kind)(x=x, y=y, z=z, u=u)
        builder.speed(speed if speed is not None else self._speed)
        builder.accel(accel if accel is not None else self._accel)
        builder.submit()

    def _next_goal_id(self) -> GoalId:
        self._goal_counter += 1
        return self._goal_counter


def _scara_status_to_matrix(status) -> np.ndarray:
    """Build a 4×4 matrix from a ScaraStatusResponse.

    SCARA pose is (x, y, z, u) where u is yaw — rotation about the world
    Z axis. The convention ``zyx_intrinsic_deg`` applies the first array
    element to Z, so yaw-only encodes as ``[u, 0, 0]``. The resulting
    matrix has the same 4×4 shape as 6-DOF arms' pose so leaves read it
    uniformly regardless of arm dof.
    """
    return transforms.euler_to_matrix(
        np.array([status.current_x, status.current_y, status.current_z], dtype=np.float64),
        [status.current_u, 0.0, 0.0],
        _POSE_RPY_CONVENTION,
    )
