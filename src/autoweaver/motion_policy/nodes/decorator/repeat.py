from autoweaver.motion_policy.nodes.decorator.base import DecoratorNode
from autoweaver.motion_policy.nodes.node import Status


class Repeat(DecoratorNode):
    """Repeat child on SUCCESS up to count times."""

    def __init__(self, count: int, child, name: str = ""):
        super().__init__(child=child, name=name)
        self.count = count
        self._completed = 0

    def on_start(self) -> Status:
        self._completed = 0
        return self._run()

    def on_running(self) -> Status:
        return self._run()

    def _run(self) -> Status:
        status = self.child.tick()
        if status == Status.FAILURE:
            self._completed = 0
            return Status.FAILURE
        if status == Status.RUNNING:
            return Status.RUNNING
        # SUCCESS
        self._completed += 1
        if self._completed >= self.count:
            self._completed = 0
            return Status.SUCCESS
        self.child.halt()
        return Status.RUNNING

    def reset(self) -> None:
        self._completed = 0
        super().reset()
