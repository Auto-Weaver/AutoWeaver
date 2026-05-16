from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

import numpy as np


GoalId = int


@runtime_checkable
class ArmBase(Protocol):
    """Common interface every robot arm must satisfy.

    Control methods are fire-and-forget at the task level: they send a
    goal to the controller and return a GoalId immediately. Communication-
    level synchronous waiting (e.g. TCP RPC ACK) is expected and
    acceptable as long as it stays well within the BT tick budget.

    Feedback is pull-style (NEXT-008): leaves call ``get_flange_pose()``
    on demand. The driver is responsible for converting the SDK's native
    pose representation into a standard 4×4 matrix; leaves never see the
    SDK's Euler convention or unit choices.
    """

    name: str

    # --- control (fire-and-forget) ---

    def move_j(self, target: Sequence[float]) -> GoalId:
        """Joint-space move. Returns a goal id usable with ``halt()``."""
        ...

    def move_l(self, target: Sequence[float]) -> GoalId:
        """Linear (Cartesian) move. Returns a goal id usable with ``halt()``."""
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

