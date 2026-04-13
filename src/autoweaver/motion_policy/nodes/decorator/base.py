from autoweaver.motion_policy.nodes.node import Status, TreeNode


class DecoratorNode(TreeNode):
    """Base class for decorators. Wraps a single child."""

    def __init__(self, child: TreeNode, name: str = ""):
        super().__init__(name=name)
        self.child = child

    def halt(self) -> None:
        self.child.halt()
        super().halt()

    def set_blackboard(self, blackboard, key_mapping=None) -> None:
        super().set_blackboard(blackboard, key_mapping)
        self.child.set_blackboard(blackboard, key_mapping)
