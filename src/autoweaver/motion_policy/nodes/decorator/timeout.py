import time

from autoweaver.motion_policy.nodes.decorator.base import DecoratorNode
from autoweaver.motion_policy.nodes.node import Status


class Timeout(DecoratorNode):
    """Fail if child doesn't complete within seconds."""

    def __init__(self, seconds: float, child, name: str = ""):
        super().__init__(child=child, name=name)
        self.seconds = seconds
        self._start_time: float | None = None

    def on_start(self) -> Status:
        self._start_time = time.monotonic()
        return self._check()

    def on_running(self) -> Status:
        return self._check()

    def _check(self) -> Status:
        if time.monotonic() - self._start_time > self.seconds:
            self.child.halt()
            self._start_time = None
            return Status.FAILURE
        status = self.child.tick()
        if status != Status.RUNNING:
            self._start_time = None
        return status

    def reset(self) -> None:
        self._start_time = None
        super().reset()
