from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any

from autoweaver.motion_policy.nodes.node import Status, TreeNode

if TYPE_CHECKING:
    pass


GoalId = int


class ActionLeaf(TreeNode):
    """Side-effecting leaf node — drives an external device.

    Lifecycle hooks (NEXT-004):
        on_start    — send goal, return RUNNING
        on_running  — read self.snapshot, decide SUCCESS / FAILURE / RUNNING
        on_halted   — call self.device.halt(goal_id) to halt the in-flight goal

    Fire-and-forget at the task level: on_start does not wait for the goal
    to physically complete. Communication-level blocking (e.g. TCP RPC ACK)
    inside device.move_*() is acceptable as long as it fits within the BT
    tick budget.
    """

    def __init__(self, device: Any, name: str = ""):
        super().__init__(name=name)
        self.device = device
        self._goal_id: GoalId | None = None

    @abstractmethod
    def on_start(self) -> Status:
        """Send goal to device, return RUNNING.

        Typical pattern:
            self._goal_id = self.device.move_j(self.target)
            return Status.RUNNING
        """

    @abstractmethod
    def on_running(self) -> Status:
        """Read self.snapshot to decide SUCCESS / FAILURE / RUNNING."""

    def on_halted(self) -> None:
        if self._goal_id is not None:
            self.device.halt(self._goal_id)
            self._goal_id = None

    def reset(self) -> None:
        self._goal_id = None
        super().reset()
