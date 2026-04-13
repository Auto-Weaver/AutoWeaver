from __future__ import annotations

import asyncio
from dataclasses import dataclass

from autoweaver.motion_policy.blackboard import Blackboard
from autoweaver.motion_policy.nodes.node import Status, TreeNode


@dataclass
class ActionResult:
    success: bool
    message: str = ""


class Action:
    """Drives a BT tree with a fixed-frequency tick loop.

    Analogous to Task in Perception Engine.
    One Action holds one tree and one Blackboard.
    """

    def __init__(
        self,
        tree: TreeNode,
        blackboard: Blackboard | None = None,
        hz: int = 50,
        name: str = "",
    ):
        self.tree = tree
        self.blackboard = blackboard or Blackboard()
        self.interval = 1.0 / hz
        self.name = name or tree.name

        self.tree.set_blackboard(self.blackboard)

    async def run(self) -> ActionResult:
        """Run the tick loop until the tree completes."""
        while True:
            status = self.tree.tick()

            if status == Status.SUCCESS:
                return ActionResult(success=True)
            if status == Status.FAILURE:
                return ActionResult(success=False, message="Tree returned FAILURE")

            await asyncio.sleep(self.interval)

    def halt(self) -> None:
        """Stop the tree immediately."""
        self.tree.halt()
