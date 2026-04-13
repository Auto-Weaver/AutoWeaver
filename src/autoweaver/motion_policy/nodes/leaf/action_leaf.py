from abc import abstractmethod

from autoweaver.motion_policy.nodes.leaf.base import LeafNode
from autoweaver.motion_policy.nodes.node import Status


class ActionLeaf(LeafNode):
    """Base class for action leaves (side effects).

    Subclass and implement on_start() and on_running().
    on_start() is called on the first tick (send goal).
    on_running() is called on subsequent ticks (check feedback).
    Override on_halted() for cleanup (cancel requests).
    """

    @abstractmethod
    def on_start(self) -> Status: ...

    @abstractmethod
    def on_running(self) -> Status: ...
