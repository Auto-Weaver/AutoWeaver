from autoweaver.motion_policy.nodes.control.base import ControlNode
from autoweaver.motion_policy.nodes.node import Status


class Fallback(ControlNode):
    """Try children in order. First SUCCESS wins."""

    def __init__(self, children, name=""):
        super().__init__(children, name=name)
        self._current_index = 0

    def on_start(self) -> Status:
        self._current_index = 0
        return self._tick_children()

    def on_running(self) -> Status:
        return self._tick_children()

    def _tick_children(self) -> Status:
        while self._current_index < len(self.children):
            status = self.children[self._current_index].tick(self._snapshot)
            if status == Status.SUCCESS:
                self._halt_from(self._current_index + 1)
                self._current_index = 0
                return Status.SUCCESS
            if status == Status.RUNNING:
                return Status.RUNNING
            self._current_index += 1
        self._current_index = 0
        return Status.FAILURE

    def _halt_from(self, index: int) -> None:
        for i in range(index, len(self.children)):
            self.children[i].halt()

    def reset(self) -> None:
        self._current_index = 0
        super().reset()
