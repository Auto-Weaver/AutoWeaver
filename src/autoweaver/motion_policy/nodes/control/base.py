from __future__ import annotations

from autoweaver.motion_policy.nodes.node import Status, TreeNode


class ControlNode(TreeNode):
    """Base class for control nodes. Manages a list of children."""

    def __init__(self, children: list[TreeNode], name: str = ""):
        super().__init__(name=name)
        self.children = children
        self._was_explicit = True

    def halt(self) -> None:
        for child in self.children:
            child.halt()
        super().halt()

    def _children_list(self) -> list[TreeNode]:
        return list(self.children)

    def set_blackboard(self, blackboard, key_mapping=None, writer_id="") -> None:
        super().set_blackboard(blackboard, key_mapping, writer_id)
        for child in self.children:
            child.set_blackboard(blackboard, key_mapping, writer_id)
