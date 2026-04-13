from autoweaver.motion_policy.nodes.decorator.base import DecoratorNode
from autoweaver.motion_policy.nodes.node import Status


class Inverter(DecoratorNode):
    """Swap SUCCESS and FAILURE. RUNNING passes through."""

    def on_start(self) -> Status:
        return self._invert(self.child.tick())

    def on_running(self) -> Status:
        return self._invert(self.child.tick())

    @staticmethod
    def _invert(status: Status) -> Status:
        if status == Status.SUCCESS:
            return Status.FAILURE
        if status == Status.FAILURE:
            return Status.SUCCESS
        return status
