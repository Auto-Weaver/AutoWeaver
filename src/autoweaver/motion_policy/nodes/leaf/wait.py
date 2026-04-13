import time

from autoweaver.motion_policy.nodes.leaf.base import LeafNode
from autoweaver.motion_policy.nodes.node import Status


class Wait(LeafNode):
    """Wait for a specified duration. Returns RUNNING until time elapsed."""

    def __init__(self, seconds: float, name: str = ""):
        super().__init__(name=name)
        self.seconds = seconds
        self._start_time: float | None = None

    def on_start(self) -> Status:
        self._start_time = time.monotonic()
        if self.seconds <= 0:
            return Status.SUCCESS
        return Status.RUNNING

    def on_running(self) -> Status:
        if time.monotonic() - self._start_time >= self.seconds:
            self._start_time = None
            return Status.SUCCESS
        return Status.RUNNING

    def on_halted(self) -> None:
        self._start_time = None

    def reset(self) -> None:
        self._start_time = None
        super().reset()
