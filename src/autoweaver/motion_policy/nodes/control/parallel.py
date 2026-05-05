from autoweaver.motion_policy.nodes.control.base import ControlNode
from autoweaver.motion_policy.nodes.node import Status


class Parallel(ControlNode):
    """Tick all children each tick. Configurable success threshold."""

    def __init__(self, children, success_threshold: int | None = None, name: str = ""):
        super().__init__(children, name=name)
        self.threshold = success_threshold if success_threshold is not None else len(children)

    def on_start(self) -> Status:
        return self._tick_all()

    def on_running(self) -> Status:
        return self._tick_all()

    def _tick_all(self) -> Status:
        success_count = 0
        failure_count = 0

        for child in self.children:
            status = child.tick(self._snapshot)
            if status == Status.SUCCESS:
                success_count += 1
            elif status == Status.FAILURE:
                failure_count += 1

        if success_count >= self.threshold:
            self._halt_running()
            return Status.SUCCESS
        if failure_count > len(self.children) - self.threshold:
            self._halt_running()
            return Status.FAILURE
        return Status.RUNNING

    def _halt_running(self) -> None:
        for child in self.children:
            if child.status == Status.RUNNING:
                child.halt()
