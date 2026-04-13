from autoweaver.motion_policy.nodes.control.base import ControlNode
from autoweaver.motion_policy.nodes.node import Status


class Premise(ControlNode):
    """Non-memory sequence: re-evaluates from the first child every tick.

    Equivalent to ReactiveSequence in BT literature. The first child
    is the premise (condition) that must hold continuously.
    """

    def on_start(self) -> Status:
        return self._tick_from_start()

    def on_running(self) -> Status:
        return self._tick_from_start()

    def _tick_from_start(self) -> Status:
        for i, child in enumerate(self.children):
            status = child.tick()
            if status == Status.FAILURE:
                self._halt_from(i + 1)
                return Status.FAILURE
            if status == Status.RUNNING:
                return Status.RUNNING
        return Status.SUCCESS

    def _halt_from(self, index: int) -> None:
        for i in range(index, len(self.children)):
            self.children[i].halt()
