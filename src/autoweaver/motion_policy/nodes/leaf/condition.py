from abc import abstractmethod

from autoweaver.motion_policy.nodes.node import Status, TreeNode


class Condition(TreeNode):
    """Base class for condition leaves (no side effects, pure read).

    Conditions return SUCCESS or FAILURE instantly. Never RUNNING.
    Subclass and implement check() which returns bool.
    """

    @abstractmethod
    def check(self) -> bool: ...

    def on_start(self) -> Status:
        return Status.SUCCESS if self.check() else Status.FAILURE

    def on_running(self) -> Status:
        return Status.SUCCESS if self.check() else Status.FAILURE
