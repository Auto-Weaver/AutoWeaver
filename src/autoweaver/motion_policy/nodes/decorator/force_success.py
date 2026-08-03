from autoweaver.motion_policy.nodes.decorator.base import DecoratorNode
from autoweaver.motion_policy.nodes.node import Status


class ForceSuccess(DecoratorNode):
    """Always return SUCCESS. RUNNING passes through."""

    def on_start(self) -> Status:
        return self._force(self.child.tick(self._snapshot, self._tick_ctx))

    def on_running(self) -> Status:
        return self._force(self.child.tick(self._snapshot, self._tick_ctx))

    @staticmethod
    def _force(status: Status) -> Status:
        if status == Status.RUNNING:
            return Status.RUNNING
        return Status.SUCCESS
