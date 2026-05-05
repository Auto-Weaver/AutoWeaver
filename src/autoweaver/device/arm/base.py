from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from autoweaver.motion_policy.world_board import WorldBoard


GoalId = int


@runtime_checkable
class ArmBase(Protocol):
    """Common interface every robot arm must satisfy.

    Control methods are fire-and-forget at the task level: they send a
    goal to the controller and return a GoalId immediately. Communication-
    level synchronous waiting (e.g. TCP RPC ACK) is expected and
    acceptable as long as it stays well within the BT tick budget.

    Feedback is published asynchronously: ``register_outputs`` declares
    the keys this instance owns on the WorldBoard, and a background
    thread started by ``start()`` pushes the latest controller state
    under those keys.
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

    # --- feedback ---

    def register_outputs(self, board: WorldBoard) -> None:
        """Register the keys this arm writes on ``board``.

        Called once during setup, before ``start()``. After registration
        the background feedback thread is allowed to write under
        ``<self.name>.*`` keys.
        """
        ...

    # --- lifecycle ---

    def start(self) -> None:
        """Connect (if needed) and start the background feedback thread."""
        ...

    def stop(self) -> None:
        """Stop the feedback thread and disconnect."""
        ...
