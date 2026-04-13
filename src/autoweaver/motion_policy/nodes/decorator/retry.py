from autoweaver.motion_policy.nodes.decorator.base import DecoratorNode
from autoweaver.motion_policy.nodes.node import Status


class Retry(DecoratorNode):
    """Retry child on FAILURE up to max_attempts times."""

    def __init__(self, max_attempts: int, child, name: str = ""):
        super().__init__(child=child, name=name)
        self.max_attempts = max_attempts
        self._attempt = 0

    def on_start(self) -> Status:
        self._attempt = 0
        return self._try()

    def on_running(self) -> Status:
        return self._try()

    def _try(self) -> Status:
        status = self.child.tick()
        if status == Status.SUCCESS:
            self._attempt = 0
            return Status.SUCCESS
        if status == Status.RUNNING:
            return Status.RUNNING
        # FAILURE
        self._attempt += 1
        if self._attempt >= self.max_attempts:
            self._attempt = 0
            return Status.FAILURE
        self.child.halt()
        return Status.RUNNING  # Will retry next tick

    def reset(self) -> None:
        self._attempt = 0
        super().reset()
