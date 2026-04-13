from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autoweaver.motion_policy.blackboard import Blackboard


class Status(Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    RUNNING = "RUNNING"
    IDLE = "IDLE"


class TreeNode(ABC):
    """Abstract base class for all BT nodes."""

    def __init__(self, name: str = ""):
        self.name = name or self.__class__.__name__
        self.status = Status.IDLE
        self._blackboard: Blackboard | None = None
        self._port_mapping: dict[str, str] = {}
        self._writer_id: str = ""

    def tick(self) -> Status:
        if self.status == Status.IDLE:
            self.status = self.on_start()
        elif self.status == Status.RUNNING:
            self.status = self.on_running()

        result = self.status
        if result != Status.RUNNING:
            self.reset()

        return result

    @abstractmethod
    def on_start(self) -> Status: ...

    @abstractmethod
    def on_running(self) -> Status: ...

    def on_halted(self) -> None:
        pass

    def halt(self) -> None:
        if self.status == Status.RUNNING:
            self.on_halted()
            self.status = Status.IDLE

    def reset(self) -> None:
        self.status = Status.IDLE

    def set_blackboard(
        self,
        blackboard: Blackboard,
        port_mapping: dict[str, str] | None = None,
        writer_id: str = "",
    ) -> None:
        self._blackboard = blackboard
        if port_mapping:
            self._port_mapping = port_mapping
        if writer_id:
            self._writer_id = writer_id

    def get_input(self, port_name: str) -> Any:
        if self._blackboard is None:
            raise RuntimeError("Blackboard not set")
        key = self._port_mapping.get(port_name, port_name)
        return self._blackboard.read(key)

    def set_output(self, port_name: str, value: Any) -> None:
        if self._blackboard is None:
            raise RuntimeError("Blackboard not set")
        key = self._port_mapping.get(port_name, port_name)
        self._blackboard.write(key, value, self._writer_id)

    # --- Operator DSL ---

    def __rshift__(self, other: TreeNode) -> TreeNode:
        """a >> b → Sequence([a, b])"""
        from autoweaver.motion_policy.nodes.control.sequence import Sequence

        left = self._flatten_children(Sequence)
        right = other._flatten_children(Sequence)
        seq = Sequence(left + right)
        seq._was_explicit = False
        return seq

    def __or__(self, other: TreeNode) -> TreeNode:
        """a | b → Fallback([a, b])"""
        from autoweaver.motion_policy.nodes.control.fallback import Fallback

        left = self._flatten_children(Fallback)
        right = other._flatten_children(Fallback)
        fb = Fallback(left + right)
        fb._was_explicit = False
        return fb

    def __and__(self, other: TreeNode) -> TreeNode:
        """a & b → Sequence([a, b]) for condition composition"""
        from autoweaver.motion_policy.nodes.control.sequence import Sequence

        left = self._flatten_children(Sequence)
        right = other._flatten_children(Sequence)
        seq = Sequence(left + right)
        seq._was_explicit = False
        return seq

    def __invert__(self) -> TreeNode:
        """~a → Inverter(a)"""
        from autoweaver.motion_policy.nodes.decorator.inverter import Inverter

        return Inverter(child=self)

    def premise(self, action: TreeNode) -> TreeNode:
        """cond.premise(action) → Premise([cond, action])"""
        from autoweaver.motion_policy.nodes.control.premise import Premise

        return Premise([self, action])

    def timeout(self, seconds: float) -> TreeNode:
        from autoweaver.motion_policy.nodes.decorator.timeout import Timeout

        return Timeout(seconds=seconds, child=self)

    def retry(self, max_attempts: int) -> TreeNode:
        from autoweaver.motion_policy.nodes.decorator.retry import Retry

        return Retry(max_attempts=max_attempts, child=self)

    def repeat(self, count: int) -> TreeNode:
        from autoweaver.motion_policy.nodes.decorator.repeat import Repeat

        return Repeat(count=count, child=self)

    def force_success(self) -> TreeNode:
        from autoweaver.motion_policy.nodes.decorator.force_success import ForceSuccess

        return ForceSuccess(child=self)

    def _flatten_children(self, node_type: type) -> list[TreeNode]:
        """Flatten chained operators: a >> b >> c → Sequence([a, b, c])."""
        if isinstance(self, node_type) and not self._was_explicit:
            return list(self.children)
        return [self]

    def _children_list(self) -> list[TreeNode]:
        return [self]
